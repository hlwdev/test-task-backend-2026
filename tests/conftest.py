"""
Тесты на настоящем Postgres (pgserver — встроенный Postgres из pip).

Запуск:  
pip install -r requirements-dev.txt 
pytest -q
"""
import os
import sys
import tempfile

import pgserver
import pytest
from flask import Flask
from sqlalchemy import event, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  
from amocrm import Customer, Lead 
from models import db, AbstractCustomer


#фейковые сущности CRM

def _fields(login):
    return {'Логин': {'id': 1, 'value': login}} if login else {}


class FakeCustomer(Customer):
    def __init__(self, id, name=None, login=None, status_id=1, contacts=(), companies=()):
        super().__init__(
            id=id, name=name or f"customer {id}", next_price=None, next_date=None,
            responsible_user_id=None, status_id=status_id, periodicity=None, created_by=None,
            updated_by=None, created_at=None, updated_at=None, closest_task_at=None,
            is_deleted=False, ltv=None, purchases_count=None, average_check=None, account_id=None,
            custom_fields_values=_fields(login),
            _embedded={'contacts': [{'id': c} for c in contacts],
                       'companies': [{'id': c} for c in companies]},
        )

    def as_dict(self):
        return {'id': self.id, 'name': self.name, 'status_id': self.status_id,
                'custom_fields_values': self.custom_fields_values}


class FakeLead(Lead):
    def __init__(self, id, name=None, login=None, status_id=1, contacts=(), broken=False):
        super().__init__(id=id, name=name or f"lead {id}", status_id=status_id,
                         custom_fields_values=_fields(login),
                         _embedded={'contacts': [{'id': c} for c in contacts]})
        self.broken = broken

    def as_dict(self):
        if self.broken:
            raise ValueError("кривая сделка")
        return {'id': self.id, 'name': self.name, 'status_id': self.status_id,
                'custom_fields_values': self.custom_fields_values}


class FakeCrm:
    """Подменяет amocrm_get: отдаёт заранее заданные списки покупателей и сделок."""

    def __init__(self):
        self.customers = []
        self.leads = []

    def __call__(self, cls, **kwargs):
        return iter(list(self.customers if cls is Customer else self.leads))



@pytest.fixture(scope="session")
def pg_uri():
    data_dir = tempfile.mkdtemp(prefix="trajectory-pg-")
    server = pgserver.get_server(data_dir, cleanup_mode="delete")
    yield server.get_uri().replace("postgresql://", "postgresql+psycopg2://", 1)


@pytest.fixture(scope="session")
def app(pg_uri):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = pg_uri
    db.init_app(app)
    return app


@pytest.fixture
def ctx(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


@pytest.fixture
def crm(monkeypatch, ctx):
    fake = FakeCrm()
    monkeypatch.setattr(main, "amocrm_get", fake)
    return fake


class SqlCounter:
    def __init__(self):
        self.stmts = []

    def count(self, verb):
        return sum(1 for s in self.stmts if s.lstrip().upper().startswith(verb))

    def reset(self):
        self.stmts.clear()


@pytest.fixture
def sql(ctx):
    counter = SqlCounter()
    engine = db.engine

    def listener(conn, cursor, statement, params, context, executemany):
        counter.stmts.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    yield counter
    event.remove(engine, "before_cursor_execute", listener)



def gen(id, login=None, name=None, **extra):
    """Элемент JSON-списка так, как он лежит в БД."""
    general = {'id': id, 'name': name or f"x {id}"}
    if login is not None:
        general['login'] = login
    general.update(extra)
    return {'general': general, 'api_value': {}}


def add_row(**fields) -> int:
    row = AbstractCustomer(**fields)
    db.session.add(row)
    db.session.commit()
    return row.id


def rows():
    db.session.expire_all()
    return db.session.query(AbstractCustomer).order_by(AbstractCustomer.id).all()


def ids(items):
    return sorted(i['general']['id'] for i in items or [])


def row_with(attr, entity_id):
    found = [r for r in rows() if entity_id in ids(getattr(r, attr))]
    assert len(found) == 1, f"{attr} {entity_id} найден в {len(found)} строках"
    return found[0]


@pytest.fixture
def raw_sql(ctx):
    def run(q, **params):
        db.session.execute(text(q), params)
        db.session.commit()
    return run
