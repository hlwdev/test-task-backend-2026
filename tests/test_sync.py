from conftest import FakeCustomer as C, FakeLead as L, add_row, gen, ids, row_with, rows

import main
from main import collect_leads_customers



def test_fresh_db_links_leads_to_customers_by_login(crm):
    crm.customers = [C(1, login="a"), C(2, login="b"), C(3)]
    crm.leads = [L(10, login="a"), L(11, login="c"), L(12)]

    collect_leads_customers()

    assert len(rows()) == 4
    r = row_with("amo_customers", 1)
    assert ids(r.amo_leads) == [10]
    assert row_with("amo_customers", 2).amo_leads in (None, [])
    assert row_with("amo_leads", 11).amo_customers in (None, [])
    assert row_with("amo_leads", 12).amo_customers in (None, [])
    assert all(3 not in ids(r.amo_customers) for r in rows())


def test_customer_found_by_lead_login(crm):
    existing = add_row(amo_leads=[gen(10, login="x")])
    crm.customers = [C(1, login="x")]
    crm.leads = [L(10, login="x")]

    collect_leads_customers()

    assert len(rows()) == 1
    r = rows()[0]
    assert r.id == existing
    assert ids(r.amo_customers) == [1] and ids(r.amo_leads) == [10]


def test_ambiguous_lead_login_creates_new_row(crm):
    add_row(amo_leads=[gen(10, login="x")])
    add_row(amo_leads=[gen(11, login="x")])
    crm.customers = [C(1, login="x")]
    crm.leads = [L(10), L(11)]

    collect_leads_customers()

    r = row_with("amo_customers", 1)
    assert r.amo_leads in (None, [])
    assert len(rows()) == 3


def test_lead_merges_two_rows(crm):
    a = add_row(amo_leads=[gen(10)])
    b = add_row(amo_customers=[gen(1, login="x")], amo_leads=[gen(20, login="x")])
    crm.customers = [C(1, login="x")]
    crm.leads = [L(10, login="x"), L(20, login="x")]

    collect_leads_customers()

    assert [r.id for r in rows()] == [a] 
    r = rows()[0]
    assert ids(r.amo_customers) == [1]
    assert ids(r.amo_leads) == [10, 20]


def test_merge_across_batches(crm):
    """Слияние в одной пачке, а сущности слитой строки встречаются в следующих."""
    main._batches.__defaults__ = (2,)
    try:
        a = add_row(amo_leads=[gen(10)])
        add_row(amo_customers=[gen(1, login="x")], amo_leads=[gen(20, login="x"), gen(21)])
        crm.customers = [C(1, login="x"), C(2, login="y"), C(3, login="z")]
        crm.leads = [L(10, login="x"), L(30), L(31), L(20, login="x"), L(21), L(32)]

        collect_leads_customers()

        r = row_with("amo_leads", 10)
        assert r.id == a
        assert ids(r.amo_customers) == [1]
        assert ids(r.amo_leads) == [10, 20, 21]
        assert len(rows()) == 6 
    finally:
        main._batches.__defaults__ = (main.BATCH_SIZE,)


def test_customer_data_updated_in_place(crm):
    crm.customers = [C(1, login="a", name="old")]
    collect_leads_customers()
    crm.customers = [C(1, login="a", name="new")]
    collect_leads_customers()

    assert len(rows()) == 1
    assert rows()[0].amo_customers[0]['general']['name'] == "new"


#запись в бд

def test_second_run_without_changes_writes_nothing(crm, sql):
    crm.customers = [C(i, login=f"l{i}") for i in range(1, 30)]
    crm.leads = [L(100 + i, login=f"l{i}") for i in range(1, 30)] + [L(500)]
    collect_leads_customers()

    sql.reset()
    collect_leads_customers()

    assert sql.count("UPDATE") == 0
    assert sql.count("INSERT") == 0
    assert sql.count("DELETE") == 0


def test_query_count_does_not_grow_with_data(crm, sql):
    """Число SELECT зависит от количества пачек, а не от количества сущностей."""
    def run(n):
        crm.customers = [C(i, login=f"l{i}") for i in range(1, n)]
        crm.leads = [L(10_000 + i, login=f"l{i}") for i in range(1, n)]
        collect_leads_customers()
        sql.reset()
        collect_leads_customers()
        return sql.count("SELECT")

    small, big = run(20), run(400)
    assert big == small, (small, big)


#чистка

def test_cleanup_removes_stale_rows_and_items(crm):
    stale = add_row(amo_customers=[gen(999, login="gone")])
    add_row(amo_customers=[gen(1, login="a")],
            amo_leads=[gen(555)],
            amo_contacts=[gen(7), gen(8)],
            amo_companies=[gen(70), gen(71)])
    crm.customers = [C(1, login="a", contacts=[7], companies=[70]), C(2, login="b", name="Удалить дубль")]
    crm.leads = [L(10, login="a")]

    collect_leads_customers()

    assert stale not in [r.id for r in rows()]
    r = row_with("amo_customers", 1)
    assert ids(r.amo_leads) == [10]
    assert ids(r.amo_contacts) == [7]
    assert ids(r.amo_companies) == [70]
    # покупатель «удалить» удален, строка без покупателей и сделок удалена
    assert all(2 not in ids(x.amo_customers) for x in rows())
    assert len(rows()) == 1


def test_broken_lead_skips_cleanup(crm):
    stale = add_row(amo_customers=[gen(999, login="gone")])
    crm.customers = [C(1, login="a")]
    crm.leads = [L(10, login="a"), L(11, broken=True), L(12)]

    collect_leads_customers()

    assert stale in [r.id for r in rows()]
    assert all(11 not in ids(r.amo_leads) for r in rows())
    row_with("amo_leads", 12)


def test_not_full_skips_statuses_and_cleanup(crm):
    stale = add_row(amo_customers=[gen(999, login="gone")])
    crm.customers = [C(1, login="a"), C(2, login="b", status_id=111349)]
    crm.leads = [L(10, login="a"), L(11, status_id=143)]

    collect_leads_customers(is_full=False)

    assert stale in [r.id for r in rows()]
    assert all(2 not in ids(r.amo_customers) for r in rows())
    assert all(11 not in ids(r.amo_leads) for r in rows())
    assert ids(row_with("amo_customers", 1).amo_leads) == [10]


def test_null_and_json_null_columns(crm, raw_sql):
    add_row(amo_customers=None, amo_leads=None)
    raw_sql("INSERT INTO abstract_customer (amo_customers, amo_leads) VALUES ('null'::jsonb, 'null'::jsonb)")
    raw_sql("""INSERT INTO abstract_customer (amo_customers, amo_leads)
               VALUES ('[{"general": {"id": 1, "login": "a", "name": "c"}}]', 'null'::jsonb)""")
    crm.customers = [C(1, login="a")]
    crm.leads = [L(10, login="a")]

    collect_leads_customers()

    assert len(rows()) == 1
    r = rows()[0]
    assert ids(r.amo_customers) == [1] and ids(r.amo_leads) == [10]
