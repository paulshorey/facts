"""Read-only Facts development infrastructure verification. No secrets fetched."""
import json
from connect import inspect_target


def main():
    directory,target,call,database=inspect_target()
    if not any(d['status']=='SUCCESS' and not d['deploymentStopped'] for d in database['activeDeployments']):
        raise ValueError('No successful active database deployment')
    plan=call('config','plan','--file',str(directory/'railway.ts'),'--json')
    if not plan['ok'] or plan['diagnostics'] or plan['changeSet']['changes']:
        raise ValueError('Facts infrastructure plan is not clean')
    schedules=call('postgres','pitr','schedule','list','--project',target['projectId'],
        '--environment',target['environmentId'],'--service',target['databaseId'],'--json')
    if sorted(s['kind'] for s in schedules)!=['DAILY','WEEKLY']:
        raise ValueError('Expected daily and weekly database backup schedules')
    print(json.dumps({'project':target['projectId'],'environment':target['environmentId'],
        'database':target['databaseId'],'volume':target['volumeId'],'bucket':target['bucketId'],
        'clean_plan':True,'database_deployment':'SUCCESS','backup_schedules':['DAILY','WEEKLY']},indent=2))


if __name__=='__main__':
    try:main()
    except Exception as error:
        raise SystemExit('Facts infrastructure verification failed: '+type(error).__name__) from None
