from fastapi import HTTPException
from sqlalchemy import and_, or_


def newest_page(db, query, model, limit, cursor):
    if cursor:
        anchor = db.execute(query.with_only_columns(model.created_at, model.id).where(model.id == cursor)).first()
        if anchor is None:
            raise HTTPException(422, "CURSOR_INVALID")
        query = query.where(or_(
            model.created_at < anchor.created_at,
            and_(model.created_at == anchor.created_at, model.id < anchor.id),
        ))
    rows = db.execute(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)).all()
    return rows[:limit], rows[limit - 1][0].id if len(rows) > limit else None
