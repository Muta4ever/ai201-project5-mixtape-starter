"""
tests/test_notifications.py — Mixtape

Tests for notification creation on song interactions.
Includes a regression test for Issue #4 (rating a song must notify the sharer).
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def data(app):
    """A sharer who shared a song, and a separate rater."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Crown Heights Anthem", artist="Borough Kings",
                    genre="rap", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_notifies_the_sharer(app, data):
    """Regression for Issue #4: rating a shared song notifies its original sharer."""
    with app.app_context():
        sharer_id = data["sharer"].id
        assert get_notifications(sharer_id) == []

        rate_song(user_id=data["rater"].id, song_id=data["song"].id, score=5)

        notifs = get_notifications(sharer_id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"
        assert "rater" in notifs[0]["body"]


def test_rating_own_song_does_not_notify(app, data):
    """A user rating their own shared song should not notify themselves."""
    with app.app_context():
        sharer_id = data["sharer"].id
        rate_song(user_id=sharer_id, song_id=data["song"].id, score=4)
        assert get_notifications(sharer_id) == []
