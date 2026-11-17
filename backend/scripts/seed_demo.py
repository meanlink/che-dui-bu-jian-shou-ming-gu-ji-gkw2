import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, Trial, TrialEnrollment, TrialObservation
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
TRIAL_DEMO_OPEN='两种检修方案盲态对比 · 盲态进行中'
TRIAL_DEMO_CLOSED='两种检修方案盲态对比 · 已揭盲'

def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        _seed_trials(db)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,Trial,TrialEnrollment,TrialObservation]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
def _seed_trials(db):
    """Idempotent demo trials: one in blinded follow-up, one closed and unblinded."""
    from src.services import trials as trial_service
    from src.schemas.trial import EnrollmentInput, ObservationsInput, ObservationInput, TrialInput, TrialStratumInput
    admin=db.scalar(select(User).where(User.role=='admin'))
    if db.scalar(select(Trial).where(Trial.name==TRIAL_DEMO_OPEN)):
        return
    sites=('东港站','西桥站')
    models=('AC-8','AC-12')
    def strata(prefix,count):
        return [TrialStratumInput(site=site,model=model,parts=[
            {'asset':f'{prefix}-{site[:2]}-{model}-{i:03}','serial':f'{prefix}{site[:2]}{model}{i:03}','batch_ref':'11 月复检'}
            for i in range(1,count+1)]) for site in sites for model in models]
    def enroll(trial_id,prefix,count):
        analyst=db.scalar(select(User).where(User.role=='analyst'))
        for site in sites:
            for model in models:
                for i in range(1,count+1):
                    trial_service.enroll(db,trial_id,EnrollmentInput(
                        asset=f'{prefix}-{site[:2]}-{model}-{i:03}',site=site,model=model,
                        serial=f'{prefix}{site[:2]}{model}{i:03}',batch_ref='11 月复检'),analyst)
    def observe(trial_id,prefix,count):
        analyst=db.scalar(select(User).where(User.role=='analyst'))
        rows=[ObservationInput(asset=f'{prefix}-{site[:2]}-{model}-{i:03}',km=18000+i*3400,
                               failed=1 if i%2==0 else 0)
              for site in sites for model in models for i in range(1,count+1)]
        trial_service.report_observations(db,trial_id,ObservationsInput(observations=rows),analyst)

    closed=trial_service.open_trial(db,TrialInput(name=TRIAL_DEMO_CLOSED,block_size=4,group_ratio='1:1',
        group_labels={'A':'按里程统一检修','B':'按可靠度窗口检修'},strata=strata('闭',40)),admin)
    enroll(closed['id'],'闭',40); observe(closed['id'],'闭',40)
    trial_service.close_trial(db,closed['id'],admin)

    live=trial_service.open_trial(db,TrialInput(name=TRIAL_DEMO_OPEN,block_size=4,group_ratio='1:1',
        group_labels={'A':'按里程统一检修','B':'按可靠度窗口检修'},strata=strata('盲',30)),admin)
    enroll(live['id'],'盲',8)


if __name__=='__main__':seed()
