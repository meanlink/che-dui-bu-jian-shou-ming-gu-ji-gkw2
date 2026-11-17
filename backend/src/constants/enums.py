from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'

class TrialStatus(str, Enum):
    open = 'open'
    closed = 'closed'


class TrialArm(str, Enum):
    A = 'A'
    B = 'B'
