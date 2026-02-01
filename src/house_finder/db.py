import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True, nullable=False)
    source = Column(String)
    address = Column(String)
    address_normalized = Column(String)
    price = Column(Integer)
    beds = Column(Integer)
    baths = Column(Float)
    property_type = Column(String)
    available_date = Column(String)
    photos = Column(Text)
    description = Column(Text)
    room_scores = Column(Text)
    avg_score = Column(Float)
    listing_pass = Column(Integer)
    llm_reasoning = Column(Text)
    date_found = Column(String)
    scored_at = Column(String)
    emailed_at = Column(String)
    reviewed_at = Column(String)

    feedbacks = relationship("Feedback", back_populates="listing")


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, ForeignKey("listings.id"))
    vote = Column(String)
    categories = Column(Text)
    reason = Column(Text)
    created_at = Column(String)

    listing = relationship("Listing", back_populates="feedbacks")


class Run(Base):
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(String)
    completed_at = Column(String)
    search_criteria = Column(Text)
    listings_found = Column(Integer)
    listings_crawled = Column(Integer)
    listings_scored = Column(Integer)
    listings_passed = Column(Integer)
    listings_emailed = Column(Integer)
    crawl_failures = Column(Integer)
    status = Column(String)
    error = Column(Text)


_engine = None
_SessionLocal = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_engine():
    global _engine
    if _engine is None:
        db_path = os.environ.get("DATABASE_PATH", "./house_finder.db")
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal()


def _ensure_column(table: str, column: str, col_type: str):
    """Add a column to an existing table if it doesn't exist yet."""
    with get_engine().connect() as conn:
        result = conn.execute(text(f"PRAGMA table_info({table})"))
        columns = [row[1] for row in result]
        if column not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            conn.commit()


def init_db():
    Base.metadata.create_all(get_engine())
    _ensure_column("listings", "reviewed_at", "TEXT")


# --- Listing helpers ---


def insert_listing(data: dict) -> int:
    with get_session() as session:
        existing = session.query(Listing).filter_by(url=data["url"]).first()
        if existing:
            return existing.id
        listing = Listing(
            url=data["url"],
            source=data.get("source"),
            address=data.get("address"),
            address_normalized=data.get("address_normalized"),
            price=data.get("price"),
            beds=data.get("beds"),
            baths=data.get("baths"),
            property_type=data.get("property_type"),
            available_date=data.get("available_date"),
            photos=data.get("photos"),
            description=data.get("description"),
            date_found=_now(),
        )
        session.add(listing)
        session.commit()
        return listing.id


def get_listing_by_url(url: str) -> Listing | None:
    with get_session() as session:
        return session.query(Listing).filter_by(url=url).first()


def get_listing_by_id(listing_id: int) -> dict | None:
    with get_session() as session:
        listing = session.query(Listing).filter_by(id=listing_id).first()
        if not listing:
            return None
        return _listing_to_dict(listing)


def _listing_to_dict(listing: Listing) -> dict:
    return {
        "id": listing.id,
        "url": listing.url,
        "source": listing.source,
        "address": listing.address,
        "address_normalized": listing.address_normalized,
        "price": listing.price,
        "beds": listing.beds,
        "baths": listing.baths,
        "property_type": listing.property_type,
        "available_date": listing.available_date,
        "photos": listing.photos,
        "description": listing.description,
        "room_scores": listing.room_scores,
        "avg_score": listing.avg_score,
        "listing_pass": listing.listing_pass,
        "llm_reasoning": listing.llm_reasoning,
        "date_found": listing.date_found,
        "scored_at": listing.scored_at,
        "emailed_at": listing.emailed_at,
        "reviewed_at": listing.reviewed_at,
    }


def update_listing_scores(
    listing_id: int,
    room_scores: str,
    avg_score: float,
    listing_pass: bool,
    llm_reasoning: str,
):
    with get_session() as session:
        listing = session.query(Listing).filter_by(id=listing_id).first()
        if listing:
            listing.room_scores = room_scores
            listing.avg_score = avg_score
            listing.listing_pass = 1 if listing_pass else 0
            listing.llm_reasoning = llm_reasoning
            listing.scored_at = _now()
            session.commit()


def mark_listing_emailed(listing_id: int):
    with get_session() as session:
        listing = session.query(Listing).filter_by(id=listing_id).first()
        if listing:
            listing.emailed_at = _now()
            session.commit()


def mark_listing_reviewed(listing_id: int):
    with get_session() as session:
        listing = session.query(Listing).filter_by(id=listing_id).first()
        if listing:
            listing.reviewed_at = _now()
            session.commit()


def get_unemailed_passed_listings() -> list[dict]:
    with get_session() as session:
        listings = (
            session.query(Listing)
            .filter(Listing.listing_pass == 1, Listing.emailed_at.is_(None))
            .all()
        )
        return [_listing_to_dict(l) for l in listings]


def get_unscored_listings() -> list[dict]:
    with get_session() as session:
        listings = (
            session.query(Listing).filter(Listing.scored_at.is_(None)).all()
        )
        return [_listing_to_dict(l) for l in listings]


def listing_exists(url: str) -> bool:
    with get_session() as session:
        return session.query(Listing).filter_by(url=url).first() is not None


def get_listing_dict_by_url(url: str) -> dict | None:
    with get_session() as session:
        listing = session.query(Listing).filter_by(url=url).first()
        if not listing:
            return None
        return _listing_to_dict(listing)


def listing_exists_by_address(normalized: str) -> bool:
    with get_session() as session:
        return (
            session.query(Listing)
            .filter_by(address_normalized=normalized)
            .first()
            is not None
        )


# --- Feedback helpers ---


def insert_feedback(
    listing_id: int,
    vote: str,
    categories: str | None = None,
    reason: str | None = None,
) -> int:
    with get_session() as session:
        fb = Feedback(
            listing_id=listing_id,
            vote=vote,
            categories=categories,
            reason=reason,
            created_at=_now(),
        )
        session.add(fb)
        session.commit()
        logger.info(f"Feedback saved: id={fb.id}, listing_id={listing_id}, vote={vote}")
        return fb.id


def get_listing_ids_with_feedback() -> set[int]:
    with get_session() as session:
        rows = session.query(Feedback.listing_id).distinct().all()
        return {row[0] for row in rows}


def get_feedback_count() -> int:
    with get_session() as session:
        return session.query(Feedback).count()


def get_recent_feedback(limit: int = 20) -> list[dict]:
    with get_session() as session:
        rows = (
            session.query(Feedback, Listing)
            .join(Listing, Feedback.listing_id == Listing.id)
            .order_by(Feedback.created_at.desc())
            .limit(limit)
            .all()
        )
        results = []
        for fb, listing in rows:
            results.append(
                {
                    "vote": fb.vote,
                    "categories": fb.categories,
                    "reason": fb.reason,
                    "photos": listing.photos,
                    "room_scores": listing.room_scores,
                    "address": listing.address,
                }
            )
        return results


# --- Run helpers ---


def create_run(search_criteria: str) -> int:
    with get_session() as session:
        run = Run(
            started_at=_now(),
            search_criteria=search_criteria,
            listings_found=0,
            listings_crawled=0,
            listings_scored=0,
            listings_passed=0,
            listings_emailed=0,
            crawl_failures=0,
            status="running",
        )
        session.add(run)
        session.commit()
        return run.id


def update_run(run_id: int, **kwargs):
    with get_session() as session:
        run = session.query(Run).filter_by(id=run_id).first()
        if run:
            for key, value in kwargs.items():
                if hasattr(run, key):
                    setattr(run, key, value)
            session.commit()


def complete_run(run_id: int, status: str, error: str | None = None):
    with get_session() as session:
        run = session.query(Run).filter_by(id=run_id).first()
        if run:
            run.completed_at = _now()
            run.status = status
            run.error = error
            session.commit()
