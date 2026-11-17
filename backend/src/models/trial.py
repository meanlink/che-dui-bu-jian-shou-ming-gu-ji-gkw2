from sqlalchemy import String, Integer, Double, ForeignKey, JSON, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class Trial(Base):
    """A blinded, stratified, permuted-block trial comparing two maintenance schemes.

    The allocation seed is stored server-side only and is never serialised by an API.
    """
    __tablename__ = 'trials'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    block_size: Mapped[int] = mapped_column(Integer, default=4)
    group_ratio: Mapped[str] = mapped_column(String(10), default='1:1')
    group_labels: Mapped[dict] = mapped_column(JSON, default=dict)
    seed_secret: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default='open')
    open_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    opened_at: Mapped[float] = mapped_column(Double)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    unblinded_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    frozen_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    frozen_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (Index('ix_trials_status', 'status'),)


class TrialStratum(Base):
    """Locked site/model stratum. The cursor is persisted so allocation resumes after a crash."""
    __tablename__ = 'trial_strata'
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'))
    site: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(120))
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    planned_parts: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (UniqueConstraint('trial_id', 'site', 'model', name='uq_trial_strata_site_model'),)


class TrialAllocation(Base):
    """A used allocation slot. Unused slots are never persisted, so they cannot be queried."""
    __tablename__ = 'trial_allocations'
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'))
    stratum_id: Mapped[int] = mapped_column(ForeignKey('trial_strata.id'))
    sequence: Mapped[int] = mapped_column(Integer)
    block_no: Mapped[int] = mapped_column(Integer)
    slot_no: Mapped[int] = mapped_column(Integer)
    arm: Mapped[str] = mapped_column(String(1))
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (UniqueConstraint('stratum_id', 'sequence', name='uq_trial_allocation_sequence'),)


class TrialEnrollment(Base):
    """One physical part enrolled once in a trial, even if it appears in several data batches."""
    __tablename__ = 'trial_enrollments'
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'))
    stratum_id: Mapped[int] = mapped_column(ForeignKey('trial_strata.id'))
    allocation_id: Mapped[int] = mapped_column(ForeignKey('trial_allocations.id'), unique=True)
    asset: Mapped[str] = mapped_column(String(160))
    site: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(120))
    serial: Mapped[str | None] = mapped_column(String(160), nullable=True)
    batch_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    request_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enrolled_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    enrolled_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (
        UniqueConstraint('trial_id', 'asset', name='uq_trial_enrollment_asset'),
        UniqueConstraint('trial_id', 'request_key', name='uq_trial_enrollment_request_key'),
        Index('ix_trial_enrollments_trial_stratum', 'trial_id', 'stratum_id'),
    )


class TrialObservation(Base):
    """Current follow-up observation for an enrollment. Old revisions are append-only below."""
    __tablename__ = 'trial_observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey('trial_enrollments.id'), unique=True)
    km: Mapped[float] = mapped_column(Double)
    failed: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    reported_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    reported_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (Index('ix_trial_observations_trial', 'trial_id'),)


class TrialObservationReport(Base):
    """Append-only observation revision. A report_key makes retries idempotent."""
    __tablename__ = 'trial_observation_reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'))
    observation_id: Mapped[int] = mapped_column(ForeignKey('trial_observations.id'))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey('trial_enrollments.id'))
    version: Mapped[int] = mapped_column(Integer)
    km: Mapped[float] = mapped_column(Double)
    failed: Mapped[int] = mapped_column(Integer)
    report_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reported_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    reported_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (
        UniqueConstraint('observation_id', 'version', name='uq_trial_observation_version'),
        UniqueConstraint('trial_id', 'report_key', name='uq_trial_observation_report_key'),
        Index('ix_trial_observation_reports_enrollment', 'enrollment_id'),
    )


class TrialSample(Base):
    """The two unblinded group datasets produced exactly once at close."""
    __tablename__ = 'trial_samples'
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'))
    arm: Mapped[str] = mapped_column(String(1))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), unique=True)
    rows: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (UniqueConstraint('trial_id', 'arm', name='uq_trial_sample_arm'),)
