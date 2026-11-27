from pydantic import BaseModel, ConfigDict, Field


class ProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sku: str = Field(pattern=r"^[A-Z0-9][A-Z0-9-]{2,31}$", examples=["USB-C-01"])
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]+$")
    quantity: int = Field(strict=True, ge=0, le=2_147_483_647)


class Product(ProductInput):
    id: int


class ErrorMessage(BaseModel):
    detail: str


class FieldError(BaseModel):
    location: list[str | int]
    message: str


class ValidationErrorMessage(ErrorMessage):
    errors: list[FieldError]
