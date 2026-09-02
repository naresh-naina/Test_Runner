from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class ExtractRule(BaseModel):
    var: str = Field(..., description="Variable name to store extracted value")
    from_: str = Field(default="json", alias="from", description="Source: 'json' or 'header'")
    path: str = Field(..., description="JSONPath like $.data.token or header name")

    model_config = {"populate_by_name": True}


class InjectRule(BaseModel):
    var: str = Field(..., description="Variable name to inject")
    into: str = Field(..., description="Where to inject: header | body | path | query")
    key: Optional[str] = Field(default=None, description="Target key name")


class ApiEndpoint(BaseModel):
    name: str = Field(..., description="Human-readable name for this endpoint")
    method: HttpMethod = Field(default=HttpMethod.GET)
    path: str = Field(..., description="URL path e.g. /api/users")
    headers: Optional[Dict[str, str]] = Field(default=None)
    body: Optional[Dict[str, Any]] = Field(default=None)
    weight: int = Field(default=1, ge=1, le=100, description="Relative frequency weight")
    timeout: float = Field(default=10.0, gt=0, le=300, description="Request timeout in seconds")
    extract: Optional[List[ExtractRule]] = Field(default=None, description="Values to extract from response")
    inject: Optional[List[InjectRule]] = Field(default=None, description="Values to inject into this request")


class HistoryDestination(str, Enum):
    local = "local"
    db = "db"


class TestConfig(BaseModel):
    base_url: str = Field(..., description="Base URL e.g. https://api.example.com")
    endpoints: List[ApiEndpoint] = Field(..., min_length=1)
    users: int = Field(default=10, ge=1, le=1000, description="Number of concurrent users")
    spawn_rate: float = Field(default=2.0, ge=0.1, description="Users spawned per second")
    duration: int = Field(default=30, ge=5, le=600, description="Test duration in seconds")
    think_time_min: float = Field(default=0.5, ge=0, description="Min wait between requests (s)")
    think_time_max: float = Field(default=2.0, ge=0, description="Max wait between requests (s)")
    history_target: HistoryDestination = Field(
        default=HistoryDestination.local,
        description="Where to save the completed run"
    )


class SaveConfigRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Human-readable name for this saved config")
    config: TestConfig


class TestStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


class RequestStat(BaseModel):
    name: str
    method: str
    num_requests: int
    num_failures: int
    avg_response_time: float
    min_response_time: float
    max_response_time: float
    p50: float
    p95: float
    p99: float
    rps: float
    failure_rate: float


class TestMetrics(BaseModel):
    status: TestStatus
    elapsed: float
    total_requests: int
    total_failures: int
    rps: float
    avg_response_time: float
    p95_response_time: float
    user_count: int
    stats: List[RequestStat]
    errors: List[Dict[str, Any]]
