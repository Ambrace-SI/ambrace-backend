"""
This module handles the application's configuration management.

It uses pydantic-settings to load configuration variables (like database
URIs and API keys) from environment variables or a .env file.

Design Decisions (First Principles):
1. All sensitive data comes from environment variables, never hardcoded.
2. Reasonable defaults where security permits.
3. Clear documentation for each setting.
"""

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.runtime_config import load_runtime_config


class Settings(BaseSettings):
    """
    Defines the application's configuration settings.

    Pydantic-settings will automatically read these variables from
    environment variables or the .env file.
    """

    DYNAMODB_TABLE_NAME: str = Field(..., min_length=3)
    # Set only for DynamoDB Local. Real DEV/PROD use the regional AWS endpoint
    # and their own table names; a local process may target DEV with DEV IAM.
    DYNAMODB_ENDPOINT_URL: str | None = None
    AWS_REGION: str = "ap-south-1"

    # Google AI Studio API Key
    GOOGLE_API_KEY: str = Field(..., description="Google AI Studio API Key")
    GEMINI_LIVE_MODEL: str = Field(
        default="gemini-3.8-live", min_length=1, pattern=r"^\S+$"
    )
    VIVA_SESSION_DURATION_MINUTES: Literal[5, 10, 15] = 5

    # The class insight for teachers (see app/services/insights/registry.py).
    # "heuristic" needs no model and costs nothing.
    CLASS_INSIGHT_PROVIDER: Literal["gemini", "heuristic"] = "gemini"
    CLASS_INSIGHT_MODEL: str = Field(
        default="gemini-3.5-flash-lite", min_length=1, pattern=r"^\S+$"
    )

    # Production Frontend URL (optional, for CORS)
    FRONTEND_URL: str | None = Field(
        default=None, description="Production frontend URL for CORS"
    )

    # Additional CORS origins (comma-separated in env var)
    # Example: CORS_ORIGINS=https://app.ambrace.in,https://ambrace.in
    CORS_ORIGINS: str = Field(
        default="",
        description="Additional CORS origins (comma-separated)",
    )

    # Amazon Cognito user pool that issues access tokens for this API. These are
    # identifiers, not secrets. Leave both unset to run with authentication
    # disabled-closed: public routes work, every protected route returns 401.
    COGNITO_USER_POOL_ID: str | None = Field(
        default=None, pattern=r"^[\w-]+_[0-9a-zA-Z]+$"
    )
    COGNITO_APP_CLIENT_ID: str | None = Field(default=None, min_length=1)

    # Configure the settings to load from a .env file
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("VIVA_SESSION_DURATION_MINUTES", mode="before")
    @classmethod
    def parse_session_duration(cls, value: Any) -> Any:
        """Parse integer configuration strings; Literal enforces supported durations."""
        # Lambda and .env values are strings; Literal[int] does not coerce them.
        # Convert only strings so numeric inputs are never truncated into valid durations.
        return int(value) if isinstance(value, str) else value

    @field_validator("COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID", mode="before")
    @classmethod
    def blank_identifier_is_unset(cls, value: Any) -> Any:
        """Treat empty deployment values as not configured."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def cognito_identifiers_come_as_a_pair(self) -> "Settings":
        """A pool without a client (or the reverse) is a misconfiguration."""
        if bool(self.COGNITO_USER_POOL_ID) != bool(self.COGNITO_APP_CLIENT_ID):
            raise ValueError(
                "COGNITO_USER_POOL_ID and COGNITO_APP_CLIENT_ID must be set together"
            )
        return self

    def __init__(self, **values):
        """Apply explicit settings overrides after resolving runtime configuration."""
        runtime_config = load_runtime_config()
        merged = {**runtime_config, **values}
        super().__init__(**merged)


# Create a single, reusable instance of the settings
# This instance will be imported by other modules to access config values.
settings = Settings()
