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


_METRICS_EXAMPLE = (
    "# HELP gateway_upstream_up 1, если последний запрос к сервису дошёл до него\n"
    "# TYPE gateway_upstream_up gauge\n"
    'gateway_upstream_up{upstream="recipes"} 1.0\n'
)


# /metrics отдаёт библиотека prometheus, поэтому описание добавляется в схему отдельно.
def metrics_path_item() -> dict:
    return {
        "get": {
            "tags": ["service"],
            "summary": "Метрики Prometheus",
            "description": (
                "Метрики gateway в текстовом формате Prometheus: HTTP-метрики по "
                "шаблону пути, `gateway_upstream_*`, `gateway_rate_limited_total`, "
                "`gateway_rate_limit_tracked_keys` и "
                "`gateway_client_ip_fallback_total`. Не пробрасывать наружу."
            ),
            "operationId": "get_metrics",
            "responses": {
                "200": {
                    "description": "Метрики в формате Prometheus.",
                    "content": {"text/plain": {"example": _METRICS_EXAMPLE}},
                },
                "500": {
                    "description": "Необработанная ошибка gateway.",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            "example": {
                                "error": {
                                    "code": "INTERNAL_ERROR",
                                    "message": "Internal server error",
                                    "details": {},
                                }
                            },
                        }
                    },
                },
            },
        }
    }
