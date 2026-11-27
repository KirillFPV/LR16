import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schema import ErrorMessage, Product, ProductInput, ValidationErrorMessage

log = logging.getLogger("uvicorn.error.inventory")

CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS products (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        sku VARCHAR(32) NOT NULL UNIQUE,
        name VARCHAR(120) NOT NULL,
        quantity INTEGER NOT NULL CHECK (quantity >= 0)
    )
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = ConnectionPool(
        kwargs={
            "host": os.environ["POSTGRES_HOST"],
            "dbname": os.environ["POSTGRES_DB"],
            "user": os.environ["POSTGRES_USER"],
            "password": os.environ["POSTGRES_PASSWORD"],
            "row_factory": dict_row,
            "connect_timeout": 3,
        },
        min_size=1,
        max_size=5,
        timeout=3,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=10)
        with pool.connection() as conn:
            conn.execute(CREATE_TABLE)
        app.state.db = pool
        yield
    finally:
        pool.close()


app = FastAPI(
    title="Inventory API",
    description="REST API for inventory products.",
    version="1.0.0",
    lifespan=lifespan,
    responses={503: {"model": ErrorMessage, "description": "Database unavailable"}},
)

INVALID = {422: {"model": ValidationErrorMessage, "description": "Invalid request"}}
NOT_FOUND = {404: {"model": ErrorMessage, "description": "Product not found"}}
CONFLICT = {409: {"model": ErrorMessage, "description": "SKU already exists"}}
ProductId = Annotated[int, Path(gt=0, le=9_223_372_036_854_775_807)]


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    log.warning("validation_error method=%s", request.method)
    message = ValidationErrorMessage(
        detail="Invalid request data.",
        errors=[
            {"location": error["loc"], "message": error["msg"]}
            for error in exc.errors()
        ],
    )
    return JSONResponse(status_code=422, content=message.model_dump())


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    log.warning("http_error method=%s status=%d", request.method, exc.status_code)
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
    )


@app.exception_handler(OperationalError)
@app.exception_handler(PoolTimeout)
async def database_unavailable(request: Request, exc: Exception):
    log.warning("database_unavailable method=%s error=%s", request.method, type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable."})


@app.get("/health", tags=["Health"], summary="Check database connectivity")
def health(request: Request):
    with request.app.state.db.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post(
    "/products",
    tags=["Products"],
    status_code=status.HTTP_201_CREATED,
    response_model=Product,
    responses={
        **INVALID,
        **CONFLICT,
        201: {"headers": {"Location": {"schema": {"type": "string"}}}},
    },
)
def create_product(data: ProductInput, request: Request, response: Response):
    try:
        with request.app.state.db.connection() as conn:
            product = conn.execute(
                """INSERT INTO products (sku, name, quantity)
                   VALUES (%s, %s, %s) RETURNING id, sku, name, quantity""",
                (data.sku, data.name, data.quantity),
            ).fetchone()
    except UniqueViolation as exc:
        raise HTTPException(409, "SKU already exists.") from exc
    log.info("product_created id=%d", product["id"])
    response.headers["Location"] = f"/products/{product['id']}"
    return product


@app.get("/products", tags=["Products"], response_model=list[Product], responses=INVALID)
def list_products(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
):
    with request.app.state.db.connection() as conn:
        return conn.execute(
            "SELECT id, sku, name, quantity FROM products ORDER BY id LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()


@app.get(
    "/products/{product_id}",
    tags=["Products"],
    response_model=Product,
    responses={**INVALID, **NOT_FOUND},
)
def get_product(product_id: ProductId, request: Request):
    with request.app.state.db.connection() as conn:
        product = conn.execute(
            "SELECT id, sku, name, quantity FROM products WHERE id = %s", (product_id,)
        ).fetchone()
    if product is None:
        raise HTTPException(404, "Product not found.")
    return product


@app.put(
    "/products/{product_id}",
    tags=["Products"],
    response_model=Product,
    responses={**INVALID, **NOT_FOUND, **CONFLICT},
)
def replace_product(product_id: ProductId, data: ProductInput, request: Request):
    try:
        with request.app.state.db.connection() as conn:
            product = conn.execute(
                """UPDATE products SET sku = %s, name = %s, quantity = %s
                   WHERE id = %s RETURNING id, sku, name, quantity""",
                (data.sku, data.name, data.quantity, product_id),
            ).fetchone()
    except UniqueViolation as exc:
        raise HTTPException(409, "SKU already exists.") from exc
    if product is None:
        raise HTTPException(404, "Product not found.")
    log.info("product_replaced id=%d", product_id)
    return product


@app.delete(
    "/products/{product_id}",
    tags=["Products"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**INVALID, **NOT_FOUND},
)
def delete_product(product_id: ProductId, request: Request):
    with request.app.state.db.connection() as conn:
        deleted = conn.execute("DELETE FROM products WHERE id = %s", (product_id,)).rowcount
    if not deleted:
        raise HTTPException(404, "Product not found.")
    log.info("product_deleted id=%d", product_id)
