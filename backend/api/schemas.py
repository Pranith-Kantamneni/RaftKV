"""Pydantic request models for the public API."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class KVRequest(BaseModel):
    op: str = Field(pattern="^(set|delete)$")
    key: str = Field(min_length=1, max_length=256)
    value: str | None = Field(default=None, max_length=100_000)

    @field_validator("key")
    @classmethod
    def key_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("key must not be blank")
        return value


class PartitionRequest(BaseModel):
    groups: list[list[str]] = Field(min_length=2)


class LatencyRequest(BaseModel):
    latency_ms: float = Field(ge=0, le=60_000)
    source: str | None = None
    target: str | None = None


class PacketLossRequest(BaseModel):
    loss: float = Field(ge=0, le=1)
    source: str | None = None
    target: str | None = None


class RandomFailureRequest(BaseModel):
    probability: float = Field(gt=0, le=1)
    interval_s: float = Field(default=2.0, ge=0.2, le=60)
    recovery_delay_s: float = Field(default=3.0, ge=0, le=120)
    max_failures: int = Field(default=3, ge=1, le=50)
    seed: int = Field(default=1234)
    duration_s: float | None = Field(default=None, gt=0, le=600)


class ExperimentRequest(BaseModel):
    scenario: str = Field(
        pattern="^(normal|follower_down|leader_crash|latency|packet_loss|partition)$"
    )
    duration_s: float = Field(default=12.0, ge=3, le=120)
    rate: float = Field(default=6.0, ge=0.5, le=100)


class BenchmarkRequest(BaseModel):
    duration_s: float = Field(default=10.0, ge=3, le=60)
    rate: float = Field(default=6.0, ge=0.5, le=100)
