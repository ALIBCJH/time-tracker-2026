"""Draining the spool into the server."""
import logging

from client import AuthError, TransientError

logger = logging.getLogger('agent.sync')


def flush_once(spool, client, limit=500):
    """Upload one batch. Returns a summary dict.

    Nothing is marked synced unless the server actually answered. A batch whose
    response is lost stays pending and is sent again — which is safe precisely
    because every record carries a client_uuid the server dedupes on. The cost
    of a duplicate upload is a wasted request; the cost of assuming success is
    a silently lost afternoon.
    """
    batch = spool.pending_batch(limit=limit)
    total = sum(len(v) for v in batch.values())
    if total == 0:
        return {'sent': 0, 'accepted': 0, 'rejected': 0, 'status': 'idle'}

    try:
        result = client.sync(batch)
    except AuthError as e:
        # Not retryable. Say so loudly — an agent quietly failing to upload for
        # a week looks exactly like an employee who did not work.
        logger.error(f'Agent is not authorised: {e}. Data is safe in the spool '
                     f'but nothing will upload until the token is replaced.')
        return {'sent': total, 'accepted': 0, 'rejected': 0, 'status': 'auth-error'}
    except TransientError as e:
        logger.info(f'Upload deferred: {e} ({total} records still queued)')
        return {'sent': total, 'accepted': 0, 'rejected': 0, 'status': 'deferred'}

    rejected = result.get('rejected', [])
    spool.mark_accepted(batch, rejected)
    accepted = sum(result.get('accepted', {}).values())

    if rejected:
        for r in rejected[:5]:
            logger.warning(f"Server rejected {r.get('kind')}[{r.get('index')}]: "
                           f"{r.get('error')}")
    logger.info(f'Uploaded {accepted}/{total} records')
    return {'sent': total, 'accepted': accepted, 'rejected': len(rejected),
            'status': 'ok'}


def flush_all(spool, client, max_batches=20):
    """Drain the backlog, a batch at a time.

    Bounded: an agent returning from a month offline uploads steadily rather
    than in one enormous transaction that times out and achieves nothing.
    """
    summary = {'batches': 0, 'accepted': 0, 'rejected': 0, 'status': 'idle'}
    for _ in range(max_batches):
        result = flush_once(spool, client)
        summary['status'] = result['status']
        if result['status'] != 'ok':
            break
        summary['batches'] += 1
        summary['accepted'] += result['accepted']
        summary['rejected'] += result['rejected']
        if result['sent'] == 0:
            break
    return summary
