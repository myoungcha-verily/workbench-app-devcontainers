from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class Base(BaseModel):
    # Fields like model_id would otherwise trigger pydantic's "model_" namespace warning.
    model_config = ConfigDict(protected_namespaces=())


class ModelStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    LOADING = "loading"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class GPUInfo(Base):
    name: Optional[str] = None
    vram_gb: int = 0
    count: int = 0
    cuda_version: str = "unknown"
    driver_version: str = "unknown"


class ModelCompat(Base):
    model_id: str
    name: str
    min_vram_gb: int
    recommended_vram_gb: int
    can_run: bool
    warning: Optional[str] = None
    sources: List[str] = []
    status: ModelStatus = ModelStatus.PENDING
    detail: str = ""
    # From Ollama /api/show once the model is ready.
    capabilities: List[str] = []
    thinking_values: List[Any] = []


class HardwareInfo(Base):
    gpu: Optional[GPUInfo] = None
    models: List[ModelCompat]


class ChatSettings(Base):
    model_id: str = ""
    # "off", "on", or a level such as "low"/"medium"/"high" when the model supports it.
    reasoning: str = "on"
    # quick / balanced / thorough: output budget and tool-step limit.
    effort: str = "balanced"
    preset: str = "workbench"
    system_prompt: str = ""
    tools: bool = True
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # OpenShell pilot: run code in a sandbox, broker SQL, read-only Workbench tools.
    governed: bool = True


class ChatMessage(Base):
    role: str  # user | assistant | tool
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)
    model_id: Optional[str] = None
    tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    thinking: Optional[str] = None
    # assistant: [{"id", "name", "arguments"}]
    tool_calls: Optional[List[Dict[str, Any]]] = None
    # tool: which call this answers
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    meta: Dict[str, Any] = {}


class ChatSession(Base):
    id: str
    title: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    messages: List[ChatMessage] = []
    model_id: str
    settings: ChatSettings = Field(default_factory=ChatSettings)
    # MCP tools enabled with find_tools, in addition to the core set.
    active_tools: List[str] = []
    metadata: Dict[str, Any] = {}


class Artifact(Base):
    id: str
    session_id: str
    version: int = 1
    type: str
    title: str
    content: str
    language: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


class EvalTask(Base):
    id: str
    name: str
    prompt: str
    category: str
    expected_output: Optional[str] = None
    metadata: Dict[str, Any] = {}


class EvalRun(Base):
    id: str
    name: str
    created_at: datetime = Field(default_factory=datetime.now)
    tasks: List[EvalTask] = []
    model_ids: List[str] = []
    status: str = "pending"
    completed: int = 0
    total: int = 0
    results: Dict[str, Any] = {}


class EvalResult(Base):
    run_id: str
    task_id: str
    model_id: str
    output: str
    latency_ms: int
    tokens: int
    success: bool
    error: Optional[str] = None
    scores: Dict[str, float] = {}
