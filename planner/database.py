from schemas.intent import StateItem
from schemas.plan import DbPlan


DB_TYPES = {
    "local": "postgres_container",
    "aws": "rds_postgres",
}


def make_db_plan(state: StateItem, target: str) -> DbPlan:
    if state.kind != "relational_db" or state.engine is None:
        raise ValueError("database_source_required")

    if state.orm != "prisma":
        raise ValueError("unsupported_database_orm")

    if target not in DB_TYPES:
        raise ValueError("unsupported_database_target")

    if state.engine == "postgresql":
        operation = "none"
    elif state.engine == "sqlite":
        operation = "sqlite_to_postgres"
    else:
        operation = "prisma_to_postgres"

    return DbPlan(
        type=DB_TYPES[target],
        source_engine=state.engine,
        target_engine="postgresql",
        orm=state.orm,
        patch=operation,
    )