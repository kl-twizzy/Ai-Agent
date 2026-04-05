from typing import Any, Optional

from pydantic import BaseModel, Field


class UserRequest(BaseModel):
    query: str
    session_id: Optional[str] = None


class AgentStep(BaseModel):
    step: int
    action: str
    description: str
    screenshot: Optional[str] = None
    outcome: Optional[str] = None
    tool_name: Optional[str] = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    llm_model: Optional[str] = None
    llm_mode: Optional[str] = None
    verification: Optional[str] = None


class PlanStep(BaseModel):
    id: str
    title: str
    status: str = "pending"
    details: Optional[str] = None


class TaskConstraints(BaseModel):
    query_type: str = "general"
    product_query: Optional[str] = None
    marketplaces: list[str] = Field(default_factory=list)
    shopping_items: list[str] = Field(default_factory=list)
    target_marketplace: Optional[str] = None
    wants_add_to_cart: bool = False
    wants_collection: bool = False
    require_new_condition: bool = False
    required_storage_gb: Optional[int] = None
    max_price: Optional[float] = None
    news_topic: Optional[str] = None
    require_news: bool = False
    target_currency: Optional[str] = None
    reference_source: Optional[str] = None
    raw_query: Optional[str] = None
    search_query: Optional[str] = None
    requested_url: Optional[str] = None
    navigation_target: Optional[str] = None
    intent: str = "search"
    sensitive_action: bool = False
    is_long_task: bool = False
    task_breakdown: list[str] = Field(default_factory=list)


class SourceRecord(BaseModel):
    kind: str
    title: str
    url: str
    snippet: Optional[str] = None


class ProductCandidate(BaseModel):
    title: str
    url: str
    marketplace: Optional[str] = None
    price: Optional[float] = None
    currency: str = "RUB"
    memory_gb: Optional[int] = None
    color: Optional[str] = None
    condition: Optional[str] = None
    seller: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    delivery: Optional[str] = None
    snippet: Optional[str] = None
    matched_constraints: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    score: float = 0.0


class NewsItem(BaseModel):
    title: str
    url: str
    summary: Optional[str] = None
    published_at: Optional[str] = None
    source: Optional[str] = None
    relevance_score: float = 0.0


class CurrencyRate(BaseModel):
    currency_code: str
    value: Optional[float] = None
    nominal: Optional[int] = None
    unit: Optional[str] = None
    date: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    snippet: Optional[str] = None


class FinalReport(BaseModel):
    summary: str = ""
    exchange_rate: Optional[CurrencyRate] = None
    best_product: Optional[ProductCandidate] = None
    product_candidates: list[ProductCandidate] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    audit_log: list[str] = Field(default_factory=list)
    constraints: Optional[TaskConstraints] = None
    completed: bool = False


class AgentResponse(BaseModel):
    success: bool
    result: str
    steps: list[AgentStep] = Field(default_factory=list)
    error: Optional[str] = None
    plan: list[PlanStep] = Field(default_factory=list)
    report: Optional[FinalReport] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
