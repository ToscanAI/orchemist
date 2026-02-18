"""Tests for the configuration system."""

import os
import tempfile
import toml
from pathlib import Path
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.orchestration_engine.config import (
    EngineConfig, QueueConfig, RetryConfig, ModelsConfig,
    PathsConfig, ResourceConfig, LoggingConfig,
    load_toml_config, merge_env_overrides, get_config,
    create_default_config
)


class TestQueueConfig:
    """Test queue configuration."""
    
    def test_default_values(self):
        """Test default configuration values."""
        config = QueueConfig()
        
        assert config.max_workers == 8
        assert config.poll_interval_seconds == 2
        assert config.stale_worker_timeout_minutes == 5
    
    def test_validation(self):
        """Test configuration validation."""
        # Valid config
        config = QueueConfig(max_workers=16, poll_interval_seconds=5)
        assert config.max_workers == 16
        
        # Invalid values should raise ValidationError
        with pytest.raises(ValidationError):
            QueueConfig(max_workers=0)  # Below minimum
        
        with pytest.raises(ValidationError):
            QueueConfig(poll_interval_seconds=0)  # Below minimum


class TestRetryConfig:
    """Test retry configuration."""
    
    def test_default_values(self):
        """Test default retry configuration."""
        config = RetryConfig()
        
        assert config.max_retries_default == 3
        assert config.backoff_base == 1
        assert config.backoff_max == 60
        assert config.circuit_breaker_threshold == 5
    
    def test_validation_limits(self):
        """Test validation of retry limits."""
        # Valid config
        config = RetryConfig(max_retries_default=5, backoff_max=120)
        assert config.max_retries_default == 5
        assert config.backoff_max == 120
        
        # Test boundaries
        with pytest.raises(ValidationError):
            RetryConfig(max_retries_default=-1)  # Negative retries


class TestModelsConfig:
    """Test models configuration."""
    
    def test_default_values(self):
        """Test default model configuration."""
        config = ModelsConfig()
        
        assert config.default_tier == "sonnet-4"
        assert config.escalation_enabled is True
        assert "haiku-4-5" in config.tier_mappings
        assert "sonnet-4" in config.tier_mappings
        assert "opus-4-6" in config.tier_mappings
    
    def test_thinking_levels(self):
        """Test thinking level mappings."""
        config = ModelsConfig()
        
        assert config.thinking_levels["haiku-4-5"] is None
        assert config.thinking_levels["sonnet-4"] == "low"
        assert config.thinking_levels["opus-4-6"] == "medium"


class TestPathsConfig:
    """Test paths configuration."""
    
    def test_path_expansion(self):
        """Test that paths are properly expanded."""
        config = PathsConfig(
            database="~/test.db",
            logs="~/logs/",
            config_file="~/config.toml"
        )
        
        # Paths should be expanded
        assert not config.database.startswith("~")
        assert not config.logs.startswith("~")
        assert not config.config_file.startswith("~")
        
        # Should contain the home directory
        home = str(Path.home())
        assert config.database.startswith(home)
        assert config.logs.startswith(home)
        assert config.config_file.startswith(home)


class TestResourceConfig:
    """Test resource configuration."""
    
    def test_default_timeout(self):
        """Test default timeout value."""
        config = ResourceConfig()
        
        assert config.default_timeout_seconds == 3600
        assert config.max_concurrent_sessions == 8
        assert config.daily_budget_usd is None  # No default budget limit
    
    def test_budget_validation(self):
        """Test budget validation."""
        # Valid budget
        config = ResourceConfig(daily_budget_usd=Decimal('100.00'))
        assert config.daily_budget_usd == Decimal('100.00')
        
        # Negative budget should fail
        with pytest.raises(ValidationError):
            ResourceConfig(daily_budget_usd=Decimal('-10.00'))


class TestEngineConfig:
    """Test complete engine configuration."""
    
    def test_default_configuration(self):
        """Test that default configuration is valid."""
        config = EngineConfig()
        
        # Check all sections are present
        assert isinstance(config.queue, QueueConfig)
        assert isinstance(config.retry, RetryConfig)
        assert isinstance(config.models, ModelsConfig)
        assert isinstance(config.paths, PathsConfig)
        assert isinstance(config.resources, ResourceConfig)
        assert isinstance(config.logging, LoggingConfig)
        
        # Check default values
        assert config.environment == "production"
        assert config.debug_mode is False
        assert config.dry_run is False
    
    def test_nested_configuration(self):
        """Test nested configuration updates."""
        config_dict = {
            "queue": {
                "max_workers": 16,
                "poll_interval_seconds": 5
            },
            "models": {
                "default_tier": "opus-4-6",
                "escalation_enabled": False
            }
        }
        
        config = EngineConfig(**config_dict)
        
        assert config.queue.max_workers == 16
        assert config.queue.poll_interval_seconds == 5
        assert config.models.default_tier == "opus-4-6"
        assert config.models.escalation_enabled is False
        
        # Other sections should have defaults
        assert config.retry.max_retries_default == 3


class TestConfigLoading:
    """Test configuration file loading."""
    
    def test_load_toml_config_missing_file(self):
        """Test loading from non-existent file."""
        result = load_toml_config("/nonexistent/config.toml")
        assert result == {}
    
    def test_load_toml_config_valid_file(self):
        """Test loading from valid TOML file."""
        config_data = {
            "queue": {
                "max_workers": 12,
                "poll_interval_seconds": 3
            },
            "retry": {
                "max_retries_default": 5
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            toml.dump(config_data, f)
            temp_path = f.name
        
        try:
            result = load_toml_config(temp_path)
            
            assert result["queue"]["max_workers"] == 12
            assert result["queue"]["poll_interval_seconds"] == 3
            assert result["retry"]["max_retries_default"] == 5
        
        finally:
            os.unlink(temp_path)
    
    def test_load_toml_config_invalid_file(self):
        """Test loading from invalid TOML file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            f.write("invalid toml [[[")
            temp_path = f.name
        
        try:
            with pytest.raises(ValueError, match="Failed to load config"):
                load_toml_config(temp_path)
        
        finally:
            os.unlink(temp_path)


class TestEnvironmentOverrides:
    """Test environment variable overrides."""
    
    def test_basic_env_override(self):
        """Test basic environment variable override."""
        # Set environment variables
        os.environ['ORCH_QUEUE_MAX_WORKERS'] = '16'
        os.environ['ORCH_RETRY_BACKOFF_BASE'] = '2'
        os.environ['ORCH_MODELS_ESCALATION_ENABLED'] = 'false'
        
        try:
            config_dict = {}
            result = merge_env_overrides(config_dict)
            
            assert result["queue"]["max_workers"] == 16
            assert result["retry"]["backoff_base"] == 2
            assert result["models"]["escalation_enabled"] is False
        
        finally:
            # Clean up environment
            for key in ['ORCH_QUEUE_MAX_WORKERS', 'ORCH_RETRY_BACKOFF_BASE', 
                       'ORCH_MODELS_ESCALATION_ENABLED']:
                os.environ.pop(key, None)
    
    def test_env_override_type_conversion(self):
        """Test type conversion for environment overrides."""
        # Test different types
        os.environ['ORCH_QUEUE_MAX_WORKERS'] = '20'  # int
        os.environ['ORCH_RESOURCES_DAILY_BUDGET_USD'] = '150.50'  # float
        os.environ['ORCH_DEBUG_MODE'] = 'true'  # bool
        os.environ['ORCH_LOGGING_LEVEL'] = 'DEBUG'  # string
        
        try:
            config_dict = {}
            result = merge_env_overrides(config_dict)
            
            assert result["queue"]["max_workers"] == 20
            assert result["resources"]["daily_budget_usd"] == 150.50
            assert result["debug_mode"] is True
            assert result["logging"]["level"] == "DEBUG"
        
        finally:
            # Clean up
            for key in ['ORCH_QUEUE_MAX_WORKERS', 'ORCH_RESOURCES_DAILY_BUDGET_USD',
                       'ORCH_DEBUG_MODE', 'ORCH_LOGGING_LEVEL']:
                os.environ.pop(key, None)
    
    def test_env_override_ignores_non_orch(self):
        """Test that non-ORCH environment variables are ignored."""
        os.environ['OTHER_VAR'] = 'should_be_ignored'
        os.environ['ORCH_INVALID'] = 'missing_section_field'
        
        try:
            config_dict = {}
            result = merge_env_overrides(config_dict)
            
            # Should be empty or only contain valid overrides
            assert "other_var" not in str(result).lower()
        
        finally:
            os.environ.pop('OTHER_VAR', None)
            os.environ.pop('ORCH_INVALID', None)


class TestGetConfig:
    """Test complete configuration loading and validation."""
    
    def test_get_config_with_valid_file(self):
        """Test loading complete configuration from file."""
        config_data = {
            "queue": {"max_workers": 10},
            "retry": {"max_retries_default": 4},
            "models": {"default_tier": "opus-4-6"}
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            toml.dump(config_data, f)
            temp_path = f.name
        
        try:
            config = get_config(temp_path)
            
            assert isinstance(config, EngineConfig)
            assert config.queue.max_workers == 10
            assert config.retry.max_retries_default == 4
            assert config.models.default_tier == "opus-4-6"
            
            # Defaults should still be present
            assert config.resources.default_timeout_seconds == 3600
        
        finally:
            os.unlink(temp_path)
    
    def test_get_config_with_env_overrides(self):
        """Test configuration with environment overrides."""
        # Create minimal config file
        config_data = {"queue": {"max_workers": 8}}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            toml.dump(config_data, f)
            temp_path = f.name
        
        # Set environment override
        os.environ['ORCH_QUEUE_MAX_WORKERS'] = '20'
        
        try:
            config = get_config(temp_path)
            
            # Environment should override file
            assert config.queue.max_workers == 20
        
        finally:
            os.unlink(temp_path)
            os.environ.pop('ORCH_QUEUE_MAX_WORKERS', None)
    
    def test_get_config_validation_error(self):
        """Test that validation errors are properly raised."""
        # Create invalid config
        config_data = {
            "queue": {"max_workers": -5}  # Invalid value
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
            toml.dump(config_data, f)
            temp_path = f.name
        
        try:
            with pytest.raises(ValueError, match="Configuration validation failed"):
                get_config(temp_path)
        
        finally:
            os.unlink(temp_path)


class TestCreateDefaultConfig:
    """Test default configuration file creation."""
    
    def test_create_default_config(self):
        """Test creating default configuration file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "test_config.toml"
            
            result_path = create_default_config(config_path)
            
            assert result_path == config_path
            assert config_path.exists()
            
            # Verify the created config is valid
            config_dict = load_toml_config(config_path)
            
            # Should contain all major sections
            assert "queue" in config_dict
            assert "retry" in config_dict
            assert "models" in config_dict
            assert "paths" in config_dict
            
            # Should be loadable as valid config
            config = EngineConfig(**config_dict)
            assert isinstance(config, EngineConfig)
    
    def test_create_default_config_creates_directory(self):
        """Test that parent directories are created."""
        with tempfile.TemporaryDirectory() as temp_dir:
            nested_path = Path(temp_dir) / "nested" / "config" / "test.toml"
            
            result_path = create_default_config(nested_path)
            
            assert result_path == nested_path
            assert nested_path.exists()
            assert nested_path.parent.exists()


# Integration Tests

class TestConfigIntegration:
    """Integration tests for the configuration system."""
    
    def test_full_config_workflow(self):
        """Test complete configuration workflow."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            
            # 1. Create default config
            create_default_config(config_path)
            
            # 2. Modify it
            config_dict = load_toml_config(config_path)
            config_dict["queue"]["max_workers"] = 12
            
            with open(config_path, 'w') as f:
                toml.dump(config_dict, f)
            
            # 3. Load with environment override
            os.environ['ORCH_RETRY_MAX_RETRIES_DEFAULT'] = '6'
            
            try:
                config = get_config(config_path)
                
                # File values
                assert config.queue.max_workers == 12
                
                # Environment override
                assert config.retry.max_retries_default == 6
                
                # Defaults
                assert config.models.default_tier == "sonnet-4"
            
            finally:
                os.environ.pop('ORCH_RETRY_MAX_RETRIES_DEFAULT', None)