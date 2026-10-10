from fastapi import APIRouter
from pydantic import BaseModel, Field

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
    response_model=HealthResponse,
    responses={200: {"content": {"application/json": {"example": {"status": "ok"}}}}},
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
