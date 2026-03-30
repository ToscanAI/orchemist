"""Configuration System for the Orchestration Engine.

Loads settings from TOML files with Pydantic validation and sensible defaults.
Configuration hierarchy: defaults → user config → environment variables.
"""

import os
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path
from typing import Dict, Any, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator
from decimal import Decimal


class QueueConfig(BaseModel):
    """Queue management configuration."""
    max_workers: int = Field(default=8, ge=1, le=32, description="Maximum concurrent workers")
    poll_interval_seconds: int = Field(default=2, ge=1, le=60, description="Queue polling interval")
    stale_worker_timeout_minutes: float = Field(default=5, gt=0, le=60, description="Stale worker detection timeout")


class RetryConfig(BaseModel):
    """Retry and error recovery configuration."""
    max_retries_default: int = Field(default=3, ge=0, le=10, description="Default max retries per task")
    backoff_base: int = Field(default=1, ge=1, le=10, description="Base delay in seconds")
    backoff_max: int = Field(default=60, ge=1, le=300, description="Maximum backoff delay")
    circuit_breaker_threshold: int = Field(default=5, ge=1, le=20, description="Consecutive failures before circuit breaker")
    circuit_breaker_reset_minutes: int = Field(default=30, ge=5, le=180, description="Circuit breaker reset timeout")


class ModelsConfig(BaseModel):
    """Model tier and escalation configuration."""
    default_tier: str = Field(default="sonnet-4", description="Default model tier")
    escalation_enabled: bool = Field(default=True, description="Enable model tier escalation on retry")
    
    # Model mappings for OpenClaw
    tier_mappings: Dict[str, str] = Field(default={
        "haiku-4-5": "anthropic/claude-haiku-4-5-20241022",
        "sonnet-4": "anthropic/claude-sonnet-4-20250514", 
        "opus-4-6": "anthropic/claude-opus-4-6"
    })
    
    # Thinking levels per tier
    thinking_levels: Dict[str, Optional[str]] = Field(default={
        "haiku-4-5": None,
        "sonnet-4": "low",
        "opus-4-6": "medium"
    })


class PathsConfig(BaseModel):
    """File and directory paths configuration."""
    database: str = Field(default="~/.orchestration-engine/engine.db", description="SQLite database path")
    logs: str = Field(default="~/.orchestration-engine/logs/", description="Log directory")
    config_file: str = Field(default="~/.orchestration-engine/config.toml", description="Configuration file path")
    
    @field_validator('database', 'logs', 'config_file')
    @classmethod
    def expand_path(cls, v):
        """Expand user home directory in paths."""
        return str(Path(v).expanduser())


class ResourceConfig(BaseModel):
    """Resource limits and budgets."""
    default_timeout_seconds: int = Field(default=3600, ge=60, le=86400, description="Default task timeout")
    max_memory_mb: Optional[int] = Field(default=None, ge=512, description="Maximum memory per task")
    daily_budget_usd: Optional[Decimal] = Field(default=None, ge=0, description="Daily spending limit")
    
    # OpenClaw resource limits  
    max_concurrent_sessions: int = Field(default=8, ge=1, le=32, description="Max OpenClaw sessions")
    session_cleanup_minutes: int = Field(default=30, ge=5, le=180, description="Session cleanup interval")


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    format: str = Field(default="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    max_file_size_mb: int = Field(default=100, ge=1, le=1000)
    backup_count: int = Field(default=5, ge=1, le=20)
    console_output: bool = Field(default=True)


class GitHubAppConfig(BaseModel):
    """GitHub App credentials configuration."""

    app_id: Optional[int] = Field(default=None, description="GitHub App numeric ID")
    private_key_path: Optional[str] = Field(
        default=None,
        description="Path to PEM private key file",
    )
    webhook_secret: Optional[str] = Field(
        default=None,
        description="HMAC webhook secret from GitHub App settings",
    )
    installation_id: Optional[int] = Field(
        default=None,
        description="Default installation ID for token exchange",
    )

    @field_validator("private_key_path")
    @classmethod
    def expand_key_path(cls, v: Optional[str]) -> Optional[str]:
        """Expand ``~`` and resolve the private key path."""
        if v is None:
            return v
        return str(Path(v).expanduser())


class EngineConfig(BaseModel):
    """Complete orchestration engine configuration."""
    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    queue: QueueConfig = Field(default_factory=QueueConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    github_app: Optional[GitHubAppConfig] = Field(
        default=None,
        description="GitHub App authentication settings",
    )

    # Meta configuration
    environment: str = Field(default="production", description="Environment: development/production")
    debug_mode: bool = Field(default=False, description="Enable debug features")
    dry_run: bool = Field(default=False, description="Dry run mode - don't execute tasks")


def load_toml_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load configuration from TOML file.
    
    Args:
        config_path: Path to config file. If None, uses default location.
        
    Returns:
        Dictionary with configuration data. Empty dict if file doesn't exist.
    """
    if config_path is None:
        config_path = Path("~/.orchestration-engine/config.toml").expanduser()
    else:
        config_path = Path(config_path).expanduser()
    
    if not config_path.exists():
        return {}
    
    try:
        with open(config_path, 'rb') as f:
            return tomllib.load(f)
    except Exception as e:
        raise ValueError(f"Failed to load config from {config_path}: {e}")


def merge_env_overrides(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Merge environment variable overrides into config.
    
    Environment variables follow pattern: ORCH_<SECTION>_<KEY>
    Example: ORCH_QUEUE_MAX_WORKERS=16
    
    Args:
        config_dict: Base configuration dictionary
        
    Returns:
        Configuration with environment overrides applied
    """
    env_prefix = "ORCH_"
    
    for key, value in os.environ.items():
        if not key.startswith(env_prefix):
            continue
            
        # Parse ORCH_QUEUE_MAX_WORKERS -> queue.max_workers
        env_key = key[len(env_prefix):].lower()
        parts = env_key.split('_')
        
        if len(parts) < 2:
            continue
            
        section = parts[0]
        field = '_'.join(parts[1:])
        
        # Initialize section if not exists
        if section not in config_dict:
            config_dict[section] = {}
            
        # Convert value to appropriate type
        if value.lower() in ('true', 'false'):
            config_dict[section][field] = value.lower() == 'true'
        elif value.isdigit():
            config_dict[section][field] = int(value)
        elif '.' in value and value.replace('.', '').isdigit():
            try:
                config_dict[section][field] = float(value)
            except ValueError:
                config_dict[section][field] = value
        else:
            config_dict[section][field] = value
    
    return config_dict


def get_config(config_path: Optional[Union[str, Path]] = None) -> EngineConfig:
    """Load and validate configuration from file and environment.
    
    Args:
        config_path: Optional path to config file
        
    Returns:
        Validated EngineConfig instance
    """
    # Load TOML file
    config_dict = load_toml_config(config_path)
    
    # Apply environment overrides
    config_dict = merge_env_overrides(config_dict)
    
    # Validate with Pydantic
    try:
        return EngineConfig(**config_dict)
    except Exception as e:
        raise ValueError(f"Configuration validation failed: {e}")


def create_default_config(config_path: Optional[Union[str, Path]] = None) -> Path:
    """Create a default configuration file.
    
    Args:
        config_path: Where to create the config file
        
    Returns:
        Path to the created config file
    """
    if config_path is None:
        config_path = Path("~/.orchestration-engine/config.toml").expanduser()
    else:
        config_path = Path(config_path).expanduser()
    
    # Create directory if it doesn't exist
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Default configuration template
    default_config = """# Orchestration Engine Configuration
# Edit this file to customize settings

[queue]
max_workers = 8
poll_interval_seconds = 2
stale_worker_timeout_minutes = 5

[retry]
max_retries_default = 3
backoff_base = 1
backoff_max = 60
circuit_breaker_threshold = 5
circuit_breaker_reset_minutes = 30

[models]
default_tier = "sonnet-4"
escalation_enabled = true

[models.tier_mappings]
"haiku-4-5" = "anthropic/claude-haiku-4-5-20241022"
"sonnet-4" = "anthropic/claude-sonnet-4-20250514"
"opus-4-6" = "anthropic/claude-opus-4-6"

[models.thinking_levels]
"haiku-4-5" = ""  # No thinking for haiku
"sonnet-4" = "low"
"opus-4-6" = "medium"

[paths]
database = "~/.orchestration-engine/engine.db"
logs = "~/.orchestration-engine/logs/"
config_file = "~/.orchestration-engine/config.toml"

[resources]
default_timeout_seconds = 3600
max_concurrent_sessions = 8
session_cleanup_minutes = 30

[logging]
level = "INFO"
console_output = true
max_file_size_mb = 100
backup_count = 5

# Environment settings
environment = "production"
debug_mode = false
dry_run = false

# [github_app]
# app_id = 12345
# private_key_path = "~/.orchestration-engine/orchemist-bot.private-key.pem"
# webhook_secret = ""   # set via ORCH_GITHUB_APP_WEBHOOK_SECRET env var
# installation_id = 67890
"""
    
    with open(config_path, 'w') as f:
        f.write(default_config)
    
    return config_path


# Global config instance
_config: Optional[EngineConfig] = None


def get_global_config() -> EngineConfig:
    """Get the global configuration instance (singleton pattern)."""
    global _config
    if _config is None:
        _config = get_config()
    return _config


def reload_config(config_path: Optional[Union[str, Path]] = None) -> None:
    """Reload the global configuration."""
    global _config
    _config = get_config(config_path)