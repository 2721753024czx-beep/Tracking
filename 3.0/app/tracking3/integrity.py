"""Read-only source verification, separate from persisted human decisions."""
from .store import fingerprint


def check(store, media):
    try:
        path=store.asset(media['path'])
    except (ValueError,KeyError):
        return dict(status='missing',valid=False,reason='源录像缺失或路径不可用，请恢复原件或重新导入')
    expected=media.get('fingerprint')
    if not expected:
        return dict(status='unverified',valid=False,reason='未登记源文件指纹，不能核实旧证据引用')
    try:
        actual=fingerprint(path)
    except OSError:
        return dict(status='missing',valid=False,reason='源录像当前不可读取')
    if actual!=expected:
        return dict(status='changed',valid=False,reason='源录像内容已改变，旧索引不能用于当前文件，请恢复原件或另行导入')
    return dict(status='valid',valid=True,reason='源录像SHA256与导入记录一致')


def require(store, media):
    result=check(store,media)
    if not result['valid']:
        raise ValueError(result['reason'])
    return result
