from pydantic import BaseModel, Field


class TrialStratumInput(BaseModel):
    site: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    parts: list[dict] = Field(default_factory=list, max_length=1000)


class TrialInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    block_size: int = Field(default=4, ge=2, le=100)
    group_ratio: str = Field(default='1:1', pattern=r'^[1-9]\d{0,2}:[1-9]\d{0,2}$')
    group_labels: dict = Field(default_factory=dict)
    strata: list[TrialStratumInput] = Field(min_length=1, max_length=50)


class EnrollmentInput(BaseModel):
    asset: str = Field(min_length=1, max_length=160)
    site: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    serial: str | None = Field(default=None, max_length=160)
    batch_ref: str | None = Field(default=None, max_length=160)
    request_key: str | None = Field(default=None, max_length=80)


class ObservationInput(BaseModel):
    asset: str = Field(min_length=1, max_length=160)
    km: float
    failed: int = Field(ge=0, le=1)
    request_key: str | None = Field(default=None, max_length=80)


class ObservationsInput(BaseModel):
    observations: list[ObservationInput] = Field(min_length=1, max_length=2000)
    request_key: str | None = Field(default=None, max_length=80)
