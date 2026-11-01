"""Save dedicated development connections locally without displaying credentials.

Run with RAILWAY_API_TOKEN from the shell (exported from ~/.secrets.sh). This reads provider
state/credentials; it does not reset passwords, create resources or deploy code.
"""
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile


def inspect_target():
    directory=Path(__file__).resolve().parent;target=json.loads((directory/'project.json').read_text())
    env={**os.environ,'RAILWAY_PROJECT_ID':target['projectId'],'RAILWAY_ENVIRONMENT_ID':target['environmentId']}
    env.pop('RAILWAY_TOKEN',None)
    if not env.get('RAILWAY_API_TOKEN', '').strip():
        raise ValueError('Missing Railway workspace token from ~/.secrets.sh')
    # SDK 3.15.0 executes env._ --version for its CLI compatibility check.
    # A Python parent otherwise leaves '_' pointing at Python instead of Railway.
    env['_']=shutil.which('railway') or 'railway'
    def call(*args):
        output=subprocess.run(['railway',*args],env=env,capture_output=True,text=True,check=True)
        return json.loads(output.stdout)
    state=call('status','--json')
    if state['id']!=target['projectId'] or state['workspaceId']!=target['workspaceId']:
        raise ValueError('Wrong Facts workspace/project')
    selected=next(e['node'] for e in state['environments']['edges'] if e['node']['id']==target['environmentId'])
    database=next(e['node'] for e in selected['serviceInstances']['edges'] if e['node']['serviceName']==target['databaseName'])
    if database['serviceId']!=target['databaseId']:
        raise ValueError('Wrong Facts database service')
    volumes=[e['node'] for e in selected['volumeInstances']['edges'] if e['node']['serviceId']==target['databaseId']]
    if (len(volumes)!=1 or volumes[0]['volume']['id']!=target['volumeId']
            or volumes[0]['mountPath']!='/var/lib/postgresql/data' or volumes[0]['isPendingDeletion']
            or volumes[0]['environmentId']!=target['environmentId']
            or volumes[0]['deletedAt'] is not None or volumes[0]['state']!='READY'):
        raise ValueError('Facts database volume identity/attachment changed')
    info=call('bucket','info','--bucket',target['bucketId'],'--json')
    if info['id']!=target['bucketId'] or info['region']!=target['bucketRegion'] or info['environmentId']!=target['environmentId']:
        raise ValueError('Wrong Facts bucket')
    return directory, target, call, database


def main():
    directory,target,call,_=inspect_target()
    bucket=call('bucket','credentials','--bucket',target['bucketId'],'--json')
    database=call('variables','--service',target['databaseId'],'--json')
    values={'FACTS_DATABASE_URL':database['DATABASE_PUBLIC_URL'],'FACTS_S3_ENDPOINT':bucket['endpoint'],
        'FACTS_S3_BUCKET':bucket['bucketName'],'FACTS_S3_ACCESS_KEY_ID':bucket['accessKeyId'],
        'FACTS_S3_SECRET_ACCESS_KEY':bucket['secretAccessKey'],'FACTS_S3_REGION':bucket['region'],
        'FACTS_S3_PREFIX':'facts-v1'}
    if any(not isinstance(v,str) or not v or '\n' in v or '\r' in v for v in values.values()):
        raise ValueError('Missing or malformed development connection')
    root=directory.parent
    with tempfile.NamedTemporaryFile(mode='w',dir=root,prefix='.env.facts-dev.',delete=False) as stream:
        temporary=Path(stream.name)
        try:
            stream.write(''.join(k+'='+json.dumps(v)+'\n' for k,v in values.items()))
            stream.flush();os.fsync(stream.fileno());temporary.replace(root/'.env.facts-dev')
        finally:temporary.unlink(missing_ok=True)
    print('Verified Facts dev identities; wrote owner-only ignored .env.facts-dev. No credential values displayed.')


if __name__=='__main__':
    try: main()
    except Exception as error:
        raise SystemExit('Facts dev connection setup failed: '+type(error).__name__+'. Inspect target identities privately.') from None
