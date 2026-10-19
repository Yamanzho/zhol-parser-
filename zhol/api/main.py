from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from zhol.api.admin import router as admin_router
from zhol.api.admin_actions import router as admin_read_router
from zhol.api.admin_actions import write_router as admin_write_router
from zhol.api.local_access import router as local_access_router
from zhol.api.local_orders import router as local_orders_router
from zhol.llm.config import check_startup
from zhol.runtime_config import validate_process_environment


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.config = check_startup()
    validate_process_environment()
    yield


app = FastAPI(title="Zhol", version="0.1.0", lifespan=lifespan)
app.include_router(local_orders_router)
app.include_router(local_access_router)
app.include_router(admin_router)
app.include_router(admin_read_router)
app.include_router(admin_write_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
