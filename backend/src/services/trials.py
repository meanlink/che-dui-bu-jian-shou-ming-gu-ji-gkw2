"""Blinded, stratified, permuted-block trial comparing two maintenance schemes.

Design notes
------------
* Opening a trial locks its site/model strata, its equal-ratio allocation blocks and a
  server-side secret seed. The seed never leaves the server and is never audited.
* Block assignments are derived lazily from HMAC(seed, trial|stratum|block). Only used
  slots are persisted, so unused allocations cannot be looked up anywhere.
* The stratum cursor (``used_count``) advances in the same transaction as the
  enrollment, so an interrupted request resumes from the next unused slot.
* Enrollment, supplementary reports and close all take a row lock on the trial row,
  which serialises concurrent enrollments inside a stratum and stops a report racing
  a close into a mixed frozen version.
"""
import hashlib
import hmac
import math
import random
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from src.config.settings import TRIAL_SEED_SECRET
from src.constants.enums import TrialStatus
from src.models import (
    Dataset,
    Trial,
    TrialAllocation,
    TrialEnrollment,
    TrialObservation,
    TrialObservationReport,
    TrialSample,
    TrialStratum,
)
from src.services.audit import record
from src.services.business import analyze
from src.utils.serialization import checksum

MIN_KM = 1.0
MAX_KM = 10_000_000.0
MAX_RETRIES = 8
DEFAULT_LABELS = {'A': '方案甲', 'B': '方案乙'}


# --------------------------------------------------------------------------- helpers
def _ratio(value):
    try:
        left, right = (int(part) for part in str(value).split(':'))
    except (ValueError, TypeError):
        raise HTTPException(422, '分组比例须为“A:B”形式的正整数') from None
    if left < 1 or right < 1:
        raise HTTPException(422, '分组比例须为正整数')
    if left != right:
        raise HTTPException(422, '盲态试验须等比分组，例如 1:1')
    return left, right


def _normalise_parts(parts):
    """Optional pre-registered part list for a stratum (used for the pending list)."""
    result = []
    seen_assets, seen_serials = set(), set()
    for part in parts or []:
        if isinstance(part, str):
            part = {'asset': part}
        if not isinstance(part, dict):
            raise HTTPException(422, '入组清单须为部件对象或资产号')
        asset = str(part.get('asset', '')).strip()
        if not asset:
            raise HTTPException(422, '入组清单中的部件缺少资产号')
        if len(asset) > 160:
            raise HTTPException(422, '部件资产号过长')
        serial = part.get('serial')
        serial = str(serial).strip() if serial not in (None, '') else None
        if serial and len(serial) > 160:
            raise HTTPException(422, '部件编号过长')
        batch_ref = part.get('batch_ref')
        batch_ref = str(batch_ref).strip() if batch_ref not in (None, '') else None
        if asset in seen_assets:
            raise HTTPException(422, f'部件 {asset} 在同一入组清单中重复')
        if serial and serial in seen_serials:
            raise HTTPException(422, f'部件编号 {serial} 重复，拒绝同号换件')
        seen_assets.add(asset)
        if serial:
            seen_serials.add(serial)
        result.append({'asset': asset, 'serial': serial, 'batch_ref': batch_ref})
    return result


def _digest(trial, stratum, purpose):
    return hmac.new(
        TRIAL_SEED_SECRET.encode('utf-8'),
        f'{trial.seed_secret}|{trial.id}|{stratum.site}|{stratum.model}|{purpose}'.encode('utf-8'),
        hashlib.sha256,
    ).digest()


def _block_length(trial, stratum, block_no):
    """The first block uses the registered length; later blocks secretly vary by one
    equal-ratio unit so the tail of a partly-filled block cannot be inferred."""
    base = trial.block_size
    extra = sum(_ratio(trial.group_ratio))
    bump = _digest(trial, stratum, f'length-{block_no}')[0] % 2
    return base + (extra if bump and block_no > 0 else 0)


def _block_arms(trial, stratum, block_no):
    rng = random.Random(int.from_bytes(_digest(trial, stratum, f'block-{block_no}')[:16], 'big'))
    left, right = _ratio(trial.group_ratio)
    unit = ['A'] * left + ['B'] * right
    arms = []
    for _ in range(max(1, _block_length(trial, stratum, block_no) // len(unit))):
        copy = list(unit)
        rng.shuffle(copy)
        arms.extend(copy)
    return arms


def _arm_for(trial, stratum, sequence):
    block_no, start = 0, 0
    while True:
        length = _block_length(trial, stratum, block_no)
        if sequence < start + length:
            slot_no = sequence - start
            return _block_arms(trial, stratum, block_no)[slot_no], block_no, slot_no
        start += length
        block_no += 1


def _label(trial, arm):
    return (trial.group_labels or DEFAULT_LABELS).get(arm) or DEFAULT_LABELS[arm]


def _summary(db, trial):
    strata = list(db.scalars(select(TrialStratum).where(TrialStratum.trial_id == trial.id).order_by(TrialStratum.id)))
    enrolled = db.scalar(select(func.count()).select_from(TrialEnrollment).where(TrialEnrollment.trial_id == trial.id)) or 0
    observed = db.scalar(select(func.count()).select_from(TrialObservation).where(TrialObservation.trial_id == trial.id)) or 0
    planned = sum(len(stratum.planned_parts or []) for stratum in strata)
    return {
        'id': trial.id,
        'key': trial.open_key,
        'name': trial.name,
        'status': trial.status,
        'block_size': trial.block_size,
        'group_ratio': trial.group_ratio,
        'group_labels': {arm: _label(trial, arm) for arm in ('A', 'B')},
        'strata': len(strata),
        'planned': planned,
        'enrolled': enrolled,
        'pending': max(0, planned - enrolled),
        'follow_up': observed,
        'missing_observations': max(0, enrolled - observed),
        'opened_at': trial.opened_at,
        'closed_at': trial.closed_at,
        'unblinded_at': trial.unblinded_at,
    }


def _samples(db, trial_id):
    return list(db.scalars(select(TrialSample).where(TrialSample.trial_id == trial_id).order_by(TrialSample.arm)))


def _groups_payload(db, trial):
    groups = []
    for sample in _samples(db, trial.id):
        dataset = db.get(Dataset, sample.dataset_id)
        groups.append({
            'arm': sample.arm,
            'group': _label(trial, sample.arm),
            'dataset_id': sample.dataset_id,
            'dataset_name': dataset.name if dataset else None,
            'rows': sample.rows,
            'records': dataset.records if dataset else [],
        })
    return groups


def _enroll_response(trial, allocation, enrollment, replayed=False):
    return {
        'trial_id': trial.id,
        'asset': enrollment.asset,
        'site': enrollment.site,
        'model': enrollment.model,
        'serial': enrollment.serial,
        'arm': allocation.arm,
        'group': _label(trial, allocation.arm),
        'enrolled_at': enrollment.enrolled_at,
        'replayed': replayed,
    }


def _replay_enrollment(db, trial_id, request_key=None, asset=None, serial=None):
    query = select(TrialEnrollment).where(TrialEnrollment.trial_id == trial_id)
    if request_key:
        existing = db.scalar(query.where(TrialEnrollment.request_key == request_key))
    else:
        existing = db.scalar(query.where(TrialEnrollment.asset == asset)) if asset else None
    if not existing:
        return None
    if asset and existing.asset != asset:
        raise HTTPException(409, '请求号已用于其他部件，请换用新的请求号')
    if serial and existing.serial and serial != existing.serial:
        raise HTTPException(409, '部件编号与已入组记录不一致，拒绝同号换件')
    trial = db.get(Trial, existing.trial_id)
    allocation = db.get(TrialAllocation, existing.allocation_id)
    return _enroll_response(trial, allocation, existing, replayed=True)


# --------------------------------------------------------------------------- open
def open_trial(db, data, user):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, '试验名称至少需要两个字符')
    if db.scalar(select(Trial).where(Trial.name == name)):
        raise HTTPException(409, '同名试验已存在')
    left, right = _ratio(data.group_ratio)
    total = left + right
    if data.block_size < total or data.block_size % total:
        raise HTTPException(422, f'区组长度须为{total}的正整数倍，才能保证逐层分段平衡')

    clean_labels = {}
    for arm in ('A', 'B'):
        value = str((data.group_labels or {}).get(arm, DEFAULT_LABELS[arm])).strip() or DEFAULT_LABELS[arm]
        if len(value) > 40:
            raise HTTPException(422, '方案名称过长')
        clean_labels[arm] = value

    seen_assets, seen_serials, seen_strata = set(), set(), set()
    strata = []
    for item in data.strata:
        site, model = item.site.strip(), item.model.strip()
        if not site or not model:
            raise HTTPException(422, '分层须同时填写站点与型号')
        if (site, model) in seen_strata:
            raise HTTPException(422, f'站点 {site} / 型号 {model} 的分层重复')
        seen_strata.add((site, model))
        parts = _normalise_parts(item.parts)
        for part in parts:
            if part['asset'] in seen_assets:
                raise HTTPException(422, f'部件 {part["asset"]} 在入组清单中跨分层重复')
            seen_assets.add(part['asset'])
            if part['serial']:
                if part['serial'] in seen_serials:
                    raise HTTPException(422, f'部件编号 {part["serial"]} 在入组清单中重复，拒绝同号换件')
                seen_serials.add(part['serial'])
        strata.append((site, model, parts))

    trial = Trial(
        name=name,
        block_size=data.block_size,
        group_ratio=f'{left}:{right}',
        group_labels=clean_labels,
        seed_secret=secrets.token_hex(32),
        status=TrialStatus.open.value,
        open_key='T-' + secrets.token_hex(3).upper(),
        created_by=user.id,
        opened_at=time.time(),
    )
    db.add(trial)
    db.flush()
    for site, model, parts in strata:
        db.add(TrialStratum(
            trial_id=trial.id, site=site, model=model,
            used_count=0, planned_parts=parts, created_at=time.time(),
        ))
    record(db, user.email, 'trial.open', {
        'id': trial.id, 'key': trial.open_key, 'strata': len(strata),
        'block_size': trial.block_size, 'group_ratio': trial.group_ratio,
    })
    db.commit()
    return _summary(db, trial)


# --------------------------------------------------------------------------- enroll
def enroll(db, trial_id, data, user):
    asset = data.asset.strip()
    site, model = data.site.strip(), data.model.strip()
    if not asset:
        raise HTTPException(422, '部件资产号不能为空')
    trial = db.get(Trial, trial_id)
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status != TrialStatus.open.value:
        raise HTTPException(409, '试验已结案，停止入组')
    replay = _replay_enrollment(db, trial_id, request_key=data.request_key,
                                asset=asset, serial=(data.serial.strip() if data.serial not in (None, '') else None))
    if replay:
        return replay

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return _enroll_once(db, trial_id, asset, site, model, data, user)
        except (OperationalError, IntegrityError) as exc:
            last_error = exc
            db.rollback()
            time.sleep(0.01 * (attempt + 1))
    replay = _replay_enrollment(db, trial_id, request_key=data.request_key, asset=asset,
                                serial=(data.serial.strip() if data.serial not in (None, '') else None))
    if replay:
        return replay
    raise HTTPException(503, '入组并发冲突，请重试') from last_error


def _enroll_once(db, trial_id, asset, site, model, data, user):
    trial = db.execute(select(Trial).where(Trial.id == trial_id).with_for_update()).scalar_one_or_none()
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status != TrialStatus.open.value:
        raise HTTPException(409, '试验已结案，停止入组')
    stratum = db.execute(select(TrialStratum).where(
        TrialStratum.trial_id == trial.id,
        TrialStratum.site == site,
        TrialStratum.model == model,
    ).with_for_update()).scalar_one_or_none()
    if not stratum:
        raise HTTPException(422, '该站点与型号未在开验时锁定分层，拒绝入组')

    planned = {part['asset']: part for part in (stratum.planned_parts or [])}
    if planned and asset not in planned:
        raise HTTPException(422, '该部件不在开验时锁定的分层入组清单中')
    serial = data.serial.strip() if data.serial not in (None, '') else None
    if planned.get(asset, {}).get('serial') and serial != planned[asset]['serial']:
        raise HTTPException(409, '部件编号与开验清单不一致，拒绝同号换件')

    existing = db.scalar(select(TrialEnrollment).where(
        TrialEnrollment.trial_id == trial.id, TrialEnrollment.asset == asset))
    if existing:
        # A retry of the same request must return the original slot, not consume a new one.
        if data.request_key and existing.request_key == data.request_key:
            return _enroll_response(trial, db.get(TrialAllocation, existing.allocation_id),
                                    existing, replayed=True)
        raise HTTPException(409, '该部件已在本试验入组，跨批次不得重复入组')
    if serial:
        holder = db.scalar(select(TrialEnrollment).where(
            TrialEnrollment.trial_id == trial.id, TrialEnrollment.serial == serial))
        if holder:
            raise HTTPException(409, f'编号 {serial} 已由部件 {holder.asset} 入组，拒绝同号换件')

    sequence = stratum.used_count
    arm, block_no, slot_no = _arm_for(trial, stratum, sequence)
    moved = db.execute(update(TrialStratum).where(
        TrialStratum.id == stratum.id,
        TrialStratum.used_count == sequence,
    ).values(used_count=sequence + 1))
    if moved.rowcount != 1:
        raise OperationalError('stratum cursor moved concurrently', None, None)

    allocation = TrialAllocation(
        trial_id=trial.id, stratum_id=stratum.id, sequence=sequence,
        block_no=block_no, slot_no=slot_no, arm=arm, created_at=time.time(),
    )
    db.add(allocation)
    db.flush()
    enrollment = TrialEnrollment(
        trial_id=trial.id, stratum_id=stratum.id, allocation_id=allocation.id,
        asset=asset, site=site, model=model, serial=serial,
        batch_ref=(data.batch_ref.strip() if data.batch_ref else None),
        request_key=data.request_key, enrolled_by=user.id, enrolled_at=time.time(),
    )
    db.add(enrollment)
    record(db, user.email, 'trial.enroll', {'trial': trial.id, 'asset': asset, 'site': site, 'model': model})
    db.flush()
    db.commit()
    return _enroll_response(trial, allocation, enrollment)


# --------------------------------------------------------------------------- observations
def report_observations(db, trial_id, data, user):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return _report_once(db, trial_id, data, user)
        except (OperationalError, IntegrityError) as exc:
            last_error = exc
            db.rollback()
            time.sleep(0.01 * (attempt + 1))
    raise HTTPException(503, '补报并发冲突，请重试') from last_error


def _report_once(db, trial_id, data, user):
    trial = db.execute(select(Trial).where(Trial.id == trial_id).with_for_update()).scalar_one_or_none()
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status != TrialStatus.open.value:
        raise HTTPException(409, '试验已结案，拒收补报')

    errors, queued, seen = [], [], set()
    for item in data.observations:
        asset = item.asset.strip()
        if asset in seen:
            errors.append(f'{asset} 在同一批补报中重复')
            continue
        seen.add(asset)
        enrollment = db.scalar(select(TrialEnrollment).where(
            TrialEnrollment.trial_id == trial.id, TrialEnrollment.asset == asset))
        if enrollment is None:
            errors.append(f'{asset} 尚未入组')
            continue
        if not math.isfinite(item.km) or not (MIN_KM <= item.km <= MAX_KM):
            errors.append(f'{asset} 里程须为1至1000万公里的有限数值')
            continue
        if item.failed not in (0, 1):
            errors.append(f'{asset} 失效标记只能为0或1')
            continue
        queued.append((asset, enrollment, item))
    if errors:
        raise HTTPException(422, '补报未通过校验，整批未保存：' + '；'.join(errors[:10]))

    applied, skipped, now = [], [], time.time()
    for asset, enrollment, item in queued:
        key = f'{data.request_key}:{asset}' if data.request_key else None
        if key and db.scalar(select(TrialObservationReport.id).where(
                TrialObservationReport.trial_id == trial.id,
                TrialObservationReport.report_key == key)):
            skipped.append({'asset': asset, 'replayed': True})
            continue
        observation = db.execute(select(TrialObservation).where(
            TrialObservation.enrollment_id == enrollment.id).with_for_update()).scalar_one_or_none()
        if observation is None:
            observation = TrialObservation(
                trial_id=trial.id, enrollment_id=enrollment.id, km=item.km, failed=item.failed,
                version=1, reported_by=user.id, reported_at=now,
            )
            db.add(observation)
            db.flush()
        else:
            observation.km = item.km
            observation.failed = item.failed
            observation.version += 1
            observation.reported_by = user.id
            observation.reported_at = now
            db.flush()
        db.add(TrialObservationReport(
            trial_id=trial.id, observation_id=observation.id, enrollment_id=enrollment.id,
            version=observation.version, km=item.km, failed=item.failed, report_key=key,
            reported_by=user.id, reported_at=now,
        ))
        applied.append({'asset': asset, 'version': observation.version, 'km': item.km, 'failed': item.failed})

    record(db, user.email, 'trial.observe', {
        'trial': trial.id, 'applied': len(applied), 'replayed': len(skipped),
    })
    db.commit()
    return {'trial_id': trial.id, 'applied': applied, 'skipped': skipped,
            'replayed': bool(skipped) and not applied}


# --------------------------------------------------------------------------- close / unblind
def close_trial(db, trial_id, user):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return _close_once(db, trial_id, user)
        except (OperationalError, IntegrityError) as exc:
            last_error = exc
            db.rollback()
            time.sleep(0.01 * (attempt + 1))
    trial = db.get(Trial, trial_id)
    if trial and trial.status == TrialStatus.closed.value:
        return {'trial': _summary(db, trial), 'groups': _groups_payload(db, trial), 'replayed': True}
    raise HTTPException(503, '结案并发冲突，请重试') from last_error


def _close_once(db, trial_id, user):
    trial = db.execute(select(Trial).where(Trial.id == trial_id).with_for_update()).scalar_one_or_none()
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status == TrialStatus.closed.value:
        return {'trial': _summary(db, trial), 'groups': _groups_payload(db, trial), 'replayed': True}
    if trial.status != TrialStatus.open.value:
        raise HTTPException(409, '当前试验状态不可结案')

    strata = {s.id: s for s in db.scalars(select(TrialStratum).where(TrialStratum.trial_id == trial.id))}
    enrollments = list(db.scalars(select(TrialEnrollment).where(
        TrialEnrollment.trial_id == trial.id).order_by(TrialEnrollment.allocation_id)))
    observations = {o.enrollment_id: o for o in db.scalars(select(TrialObservation).where(
        TrialObservation.trial_id == trial.id))}
    pending = [part['asset'] for stratum in strata.values() for part in (stratum.planned_parts or [])
               if part['asset'] not in {e.asset for e in enrollments}]
    missing = [e.asset for e in enrollments if e.id not in observations]
    if not enrollments:
        raise HTTPException(409, '尚无入组部件，不能结案')
    if pending:
        raise HTTPException(409, f'仍有 {len(pending)} 件待入组，结案会遗漏已锁定清单：'
                                 + '、'.join(pending[:10]))
    if missing:
        raise HTTPException(409, f'仍有 {len(missing)} 件未登记里程和失效，观测未齐不结案：'
                                 + '、'.join(missing[:10]))

    allocations = {a.id: a for a in db.scalars(select(TrialAllocation).where(
        TrialAllocation.trial_id == trial.id))}
    frozen_at = time.time()
    rows = []
    for enrollment in enrollments:
        allocation = allocations[enrollment.allocation_id]
        observation = observations[enrollment.id]
        stratum = strata[enrollment.stratum_id]
        rows.append({
            'asset': enrollment.asset,
            'serial': enrollment.serial,
            'site': stratum.site,
            'model': stratum.model,
            'arm': allocation.arm,
            'group': _label(trial, allocation.arm),
            'km': observation.km,
            'failed': observation.failed,
            'observation_version': observation.version,
            'enrolled_at': enrollment.enrolled_at,
            'reported_at': observation.reported_at,
        })
    rows.sort(key=lambda row: (row['site'], row['model'], row['asset']))

    trial.status = TrialStatus.closed.value
    trial.closed_at = frozen_at
    trial.unblinded_at = frozen_at
    trial.frozen_at = frozen_at
    trial.frozen_snapshot = {
        'frozen_at': frozen_at,
        'counts': {'enrolled': len(rows), 'observed': len(rows), 'strata': len(strata)},
        'rows': rows,
    }

    existing = {sample.arm: sample for sample in _samples(db, trial.id)}
    created = []
    for arm in ('A', 'B'):
        if arm in existing:
            created.append(existing[arm])
            continue
        arm_rows = [{k: row[k] for k in ('asset', 'site', 'model', 'km', 'failed')}
                    for row in rows if row['arm'] == arm]
        dataset = Dataset(
            name=f'{trial.name} · 揭盲{_label(trial, arm)}组（试验{trial.id}）'.strip()[:160],
            source=f'trial-{trial.id}-{arm}.json',
            records=arm_rows,
            checksum=checksum(arm_rows),
            created_at=frozen_at,
        )
        db.add(dataset)
        db.flush()
        sample = TrialSample(
            trial_id=trial.id, arm=arm, dataset_id=dataset.id,
            rows=len(arm_rows), created_at=frozen_at,
        )
        db.add(sample)
        db.flush()
        created.append(sample)

    record(db, user.email, 'trial.close', {
        'trial': trial.id, 'enrolled': len(rows),
        'datasets': [sample.dataset_id for sample in created],
    })
    db.commit()
    return {'trial': _summary(db, trial), 'groups': _groups_payload(db, trial), 'replayed': False}


def groups(db, trial_id):
    trial = db.get(Trial, trial_id)
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status != TrialStatus.closed.value:
        raise HTTPException(409, '盲态试验结案后才可揭盲，盲态期间拒绝按组估计')
    return {'trial': _summary(db, trial), 'groups': _groups_payload(db, trial)}


def estimates(db, trial_id, model=None, reliability=0.9):
    trial = db.get(Trial, trial_id)
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status != TrialStatus.closed.value:
        raise HTTPException(409, '盲态期间拒绝按组估计；请先由负责人结案揭盲')
    try:
        reliability = float(reliability)
    except (TypeError, ValueError):
        raise HTTPException(422, '目标可靠度必须是数值') from None
    strata = list(db.scalars(select(TrialStratum).where(TrialStratum.trial_id == trial.id)))
    if model is None:
        ranked = sorted(strata, key=lambda s: len(s.planned_parts or []), reverse=True)
        model = ranked[0].model if ranked else None
    arms = []
    for group in _groups_payload(db, trial):
        records = [row for row in group['records'] if model is None or row.get('model') == model]
        try:
            result = analyze(records, {'model': model, 'reliability': reliability})
            arms.append({'arm': group['arm'], 'group': group['group'],
                         'dataset_id': group['dataset_id'], 'rows': len(records), 'result': result})
        except ValueError as exc:
            arms.append({'arm': group['arm'], 'group': group['group'],
                         'dataset_id': group['dataset_id'], 'rows': len(records), 'error': str(exc)})
    return {'trial': _summary(db, trial), 'model': model,
            'reliability': reliability, 'arms': arms}


# --------------------------------------------------------------------------- read models
def detail(db, trial_id, user=None):
    trial = db.get(Trial, trial_id)
    if not trial:
        raise HTTPException(404, '试验不存在')
    strata = list(db.scalars(select(TrialStratum).where(
        TrialStratum.trial_id == trial.id).order_by(TrialStratum.id)))
    enrollments = list(db.scalars(select(TrialEnrollment).where(
        TrialEnrollment.trial_id == trial.id).order_by(TrialEnrollment.allocation_id)))
    allocations = {a.id: a for a in db.scalars(select(TrialAllocation).where(
        TrialAllocation.trial_id == trial.id))}
    observations = {o.enrollment_id: o for o in db.scalars(select(TrialObservation).where(
        TrialObservation.trial_id == trial.id))}
    by_stratum = {}
    for enrollment in enrollments:
        by_stratum.setdefault(enrollment.stratum_id, []).append(enrollment)

    # During the blind phase only trial staff (admin/analyst) may see each enrolled
    # part's scheme; viewers keep the blinded view until the whole trial is unblinded.
    reveal_arms = trial.status == TrialStatus.closed.value or (
        user is not None and user.role in ('admin', 'analyst'))
    strata_payload, pending_payload = [], []
    for stratum in strata:
        members = by_stratum.get(stratum.id, [])
        enrolled_assets = {member.asset for member in members}
        pending = [part for part in (stratum.planned_parts or []) if part['asset'] not in enrolled_assets]
        follow_up = []
        for member in members:
            allocation = allocations[member.allocation_id]
            observation = observations.get(member.id)
            follow_up.append({
                'asset': member.asset,
                'serial': member.serial,
                'batch_ref': member.batch_ref,
                'site': stratum.site,
                'model': stratum.model,
                'arm': allocation.arm if reveal_arms else None,
                'group': _label(trial, allocation.arm) if reveal_arms else None,
                'enrolled_at': member.enrolled_at,
                'observation': None if observation is None else {
                    'km': observation.km, 'failed': observation.failed,
                    'version': observation.version, 'reported_at': observation.reported_at,
                },
            })
        strata_payload.append({
            'id': stratum.id, 'site': stratum.site, 'model': stratum.model,
            'planned': len(stratum.planned_parts or []),
            'enrolled': len(members), 'pending': len(pending),
            'pending_parts': pending, 'follow_up': follow_up,
        })
        pending_payload.extend({**part, 'site': stratum.site, 'model': stratum.model} for part in pending)

    return {
        'trial': _summary(db, trial),
        'strata': strata_payload,
        'pending': pending_payload,
        'follow_up': [item for stratum in strata_payload for item in stratum['follow_up']],
        'groups': _groups_payload(db, trial) if trial.status == TrialStatus.closed.value else [],
        'can_enroll': trial.status == TrialStatus.open.value,
        'can_close': (trial.status == TrialStatus.open.value and bool(enrollments)
                      and not pending_payload
                      and all(e.id in observations for e in enrollments)),
    }


def listing(db):
    return [_summary(db, trial) for trial in db.scalars(select(Trial).order_by(Trial.id.desc()))]
