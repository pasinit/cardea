"""Test-only HTTP/OpenAPI fit fixture, never mounted in either product API."""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="Cardea transport fit fixture", docs_url=None, redoc_url=None)
security = HTTPBearer()


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: Annotated[str, Field(min_length=1, max_length=64)]


def fixture_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> None:
    # A dependency-hook probe only. This fixed fictional value is neither a
    # production credential nor a proposed Cardea authentication mechanism.
    if credentials.credentials != "fictional-fit-proof":
        raise HTTPException(status_code=401, detail="invalid_fit_proof")


@app.post("/fit/echo", response_model=Echo, dependencies=[Depends(fixture_auth)])
def echo(body: Echo) -> Echo:
    return body
