from engine.game_session import GameSession


def test_periodic_and_urgent_meta_events_schedule_each_handler_once():
    session = GameSession({'script_id': 'meta-dispatch-test', 'settings': {}}, object())
    session.turn_number = 5
    session.current_state = {
        '_pending_lore_evolution': True,
    }
    scheduled = []

    def capture_task(coroutine):
        scheduled.append(coroutine.cr_code.co_name)
        coroutine.close()

    session._schedule_background_task = capture_task

    session._dispatch_meta_events({'route': {'expand_story': True}}, {},
                                  {'type': 'freeform', 'text': '观察'})

    assert scheduled.count('_handle_story_director') == 1
    assert scheduled.count('_handle_lorebook_evolution') == 1
    assert '_pending_story_director' not in session.current_state
    assert '_pending_lore_evolution' not in session.current_state
    trackers = session.current_state['meta_event_trackers']
    assert {'story_director', 'story_director_flag',
            'lorebook_evolution', 'lorebook_evolution_urgent'} <= trackers.keys()


def test_urgent_story_director_runs_between_periodic_turns():
    session = GameSession({'script_id': 'meta-dispatch-test', 'settings': {}}, object())
    session.turn_number = 6
    session.current_state = {}
    scheduled = []

    def capture_task(coroutine):
        scheduled.append(coroutine.cr_code.co_name)
        coroutine.close()

    session._schedule_background_task = capture_task

    session._dispatch_meta_events({'route': {'expand_story': True}}, {},
                                  {'type': 'freeform', 'text': '观察'})

    assert scheduled.count('_handle_story_director') == 1
    assert '_pending_story_director' not in session.current_state
    assert 'story_director_flag' in session.current_state['meta_event_trackers']
    assert 'story_director' not in session.current_state['meta_event_trackers']
