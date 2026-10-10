from fastapi import APIRouter
from pydantic import BaseModel, Field

from gateway.errors import ErrorResponse

router = APIRouter()


class HealthResponse(BaseModel):
    status: str = Field(description="Всегда `ok`, пока процесс gateway жив.")


# Это liveness самого gateway: недоступность MS-4 не должна его перезапускать.
@router.get(
    "/health",
    summary="Проверка живости gateway",
    description=(
        "Возвращает 200, если процесс gateway работает. Доступность "
        "бизнес-сервисов не проверяется: она видна в метрике "
        "`gateway_upstream_up`."
    ),
    tags=["service"],
    response_model=HealthResponse,
    responses={
        200: {
            "description": "Процесс gateway жив.",
            "content": {"application/json": {"example": {"status": "ok"}}},
        },
        500: {
            "model": ErrorResponse,
            "description": "Необработанная ошибка gateway.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "Internal server error",
                            "details": {},
                        }
                    }
                }
            },
        },
    },
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
