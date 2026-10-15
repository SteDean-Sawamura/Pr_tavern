import asyncio
import copy
from contextvars import ContextVar
import logging

from engine.review_evidence import review_state


_CURRENT = ContextVar("turn_publication", default=None)
logger = logging.getLogger(__name__)


class TurnPublication:
    def __init__(self, owner):
        self.owner = owner
        self.status = "pending"
        self.jobs = []
        self.reviewed = None

    def remember_review(self, narrative, state):
        self.reviewed = (narrative, copy.deepcopy(review_state(state)))

    def matches_review(self, narrative, state):
        return self.reviewed is None or self.reviewed == (narrative, review_state(state))

    def defer(self, coroutine, kind):
        if self.status == "committed":
            return False
        if self.status == "aborted":
            coroutine.close()
        else:
            self.jobs.append((kind, coroutine))
        return True

    def discard(self):
        if self.status != "pending":
            return
        self.status = "aborted"
        for _, coroutine in self.jobs:
            coroutine.close()
        self.jobs.clear()
        self._detach()

    def _detach(self):
        self.reviewed = None
        if getattr(self.owner, "_pending_turn_publication", None) is self:
            self.owner._pending_turn_publication = None
        if _CURRENT.get() is self:
            _CURRENT.set(None)

    def publish(self):
        if self.status != "pending":
            return
        self.status = "committed"
        jobs, self.jobs = self.jobs, []
        self._detach()
        token = _CURRENT.set(None)
        previous_index = None
        try:
            for kind, coroutine in jobs:
                scheduled = coroutine
                try:
                    if kind == "index":
                        scheduled = _after_index(previous_index, coroutine)
                        task = self.owner._schedule_index_task(scheduled)
                        if task is not None:
                            task.add_done_callback(lambda completed, inner=coroutine: inner.close())
                            previous_index = task
                    else:
                        task = self.owner._schedule_background_task(scheduled)
                    if task is None:
                        scheduled.close()
                        coroutine.close()
                except Exception:
                    scheduled.close()
                    coroutine.close()
                    logger.exception("Committed turn background publication failed")
        finally:
            _CURRENT.reset(token)


async def _after_index(previous, coroutine):
    if previous is not None:
        await asyncio.gather(asyncio.shield(previous), return_exceptions=True)
    return await coroutine


def current_publication(owner):
    publication = _CURRENT.get()
    return publication if publication is not None and publication.owner is owner else None


def begin_publication(owner):
    publication = current_publication(owner)
    if publication is not None and publication.status == "pending":
        return publication
    publication = TurnPublication(owner)
    owner._pending_turn_publication = publication
    _CURRENT.set(publication)
    return publication


def publish_pending(owner):
    publication = getattr(owner, "_pending_turn_publication", None)
    if publication is not None:
        publication.publish()
