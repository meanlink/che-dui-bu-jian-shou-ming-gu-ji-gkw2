from fastapi import APIRouter, Depends, Query
from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.schemas.trial import TrialInput, EnrollmentInput, ObservationsInput
from src.services import trials

router = APIRouter(prefix='/trials')


@router.get('')
def listing(db=Depends(get_db), u=Depends(current_user)):
    return trials.listing(db)


@router.post('', status_code=201)
def open_trial(data: TrialInput, db=Depends(get_db), u=Depends(can_write)):
    return trials.open_trial(db, data, u)


@router.get('/{id}')
def detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    return trials.detail(db, id, u)


@router.post('/{id}/enrollments', status_code=201)
def enroll(id: int, data: EnrollmentInput, db=Depends(get_db), u=Depends(can_write)):
    return trials.enroll(db, id, data, u)


@router.post('/{id}/observations')
def observe(id: int, data: ObservationsInput, db=Depends(get_db), u=Depends(can_write)):
    return trials.report_observations(db, id, data, u)


@router.post('/{id}/close')
def close(id: int, db=Depends(get_db), u=Depends(admin_only)):
    return trials.close_trial(db, id, u)


@router.get('/{id}/groups')
def groups(id: int, db=Depends(get_db), u=Depends(current_user)):
    return trials.groups(db, id)


@router.get('/{id}/estimates')
def estimates(id: int, model: str | None = None,
              reliability: float = Query(default=0.9),
              db=Depends(get_db), u=Depends(current_user)):
    return trials.estimates(db, id, model=model, reliability=reliability)
