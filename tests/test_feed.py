"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed.
Includes a regression test for Issue #2 (yesterday's plays must not appear today).
"""

from datetime import datetime, timedelta, timezone

import pytest
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def data(app):
    """A viewer with one friend who shares a song."""
    with app.app_context():
        viewer = User(username="viewer", email="viewer@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([viewer, friend])
        db.session.flush()
        db.session.execute(friendships.insert().values(user_id=viewer.id, friend_id=friend.id))

        song = Song(title="Some Song", artist="Some Artist", genre="rap", shared_by=friend.id)
        db.session.add(song)
        db.session.commit()
        yield {"viewer": viewer, "friend": friend, "song": song}


def test_friend_who_listened_yesterday_is_excluded(app, data):
    """Regression for Issue #2: a play before today's midnight must not appear."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Friend's only play was yesterday at 23:00 — the previous calendar day.
        db.session.add(ListeningEvent(
            user_id=data["friend"].id, song_id=data["song"].id,
            listened_at=start_today - timedelta(hours=1)))
        db.session.commit()

        feed = get_friends_listening_now(data["viewer"].id)
        assert feed == []


def test_friend_who_listened_today_is_included(app, data):
    """A play after today's midnight should appear."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        db.session.add(ListeningEvent(
            user_id=data["friend"].id, song_id=data["song"].id,
            listened_at=start_today + timedelta(minutes=1)))
        db.session.commit()

        feed = get_friends_listening_now(data["viewer"].id)
        assert len(feed) == 1
        assert feed[0]["friend"]["username"] == "friend"
