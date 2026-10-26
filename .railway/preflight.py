"""Read-only identity/volume preflight and pinned-plan creation; never applies."""
import argparse
import json
from pathlib import Path
from connect import inspect_target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    directory,target,call,_=inspect_target()
    plan_path=args.out.resolve()
    # The CLI may put provider metadata in plan files. Keep them in ignored storage.
    if not plan_path.is_relative_to(directory.parent/'storage'):
        raise ValueError('Save reviewed plans under ignored project storage')
    if plan_path.exists():raise ValueError('Use a new plan filename')
    plan_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    result=call('config','plan','--file',str(directory/'railway.ts'),'--json','--out',str(plan_path))
    if not result['ok'] or result['diagnostics']:
        raise ValueError('Plan has diagnostics; do not apply')
    changes=result['changeSet']['changes']
    if any(c.get('severity')!='safe' for c in changes):
        raise ValueError('Plan contains non-safe changes; do not apply')
    print(json.dumps({'project':target['projectId'],'environment':target['environmentId'],
        'volume':target['volumeId'],'plan':str(plan_path),'changes':changes,
        'requires_operator_review_before_apply':True},indent=2))


if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit('Facts apply preflight failed: '+type(error).__name__) from None
