import logging
from collections import defaultdict
from typing import Dict, Iterable, Iterator, Optional

from sqlalchemy import case, column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB

from abstract_customer import dict_compare_merge, get_customer_json, get_lead_json
from amocrm import Customer, COMPANIES, CONTACTS, Lead, amocrm_get
from models import db, AbstractCustomer

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# JSON-поле строки -> ключ в ids_map
NESTED = (("amo_customers", "customers"), ("amo_leads", "leads"),
          ("amo_contacts", "contacts"), ("amo_companies", "companies"))


def _batches(items: Iterable, size: int = BATCH_SIZE) -> Iterator[list]:
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _load_index(column_name: str, key: str) -> Dict:
    """Одним запросом: значение general.<key> из JSON-списка column_name -> множество id строк."""
    col = getattr(AbstractCustomer, column_name)
    # Если в поле не массив (NULL или JSON null) — подставляем пустой массив, чтобы запрос не упал
    array = case((func.jsonb_typeof(col) == "array", col), else_=func.jsonb_build_array())
    # Каждый элемент JSON-списка — отдельная строка результата
    elem = func.jsonb_array_elements(array).table_valued(column("value", JSONB)).lateral("e")
    value = elem.c.value[("general", key)].astext
    rows = db.session.execute(
        select(value, AbstractCustomer.id)
        .select_from(AbstractCustomer)
        .join(elem, true())
        .where(value.is_not(None))
    )
    index = defaultdict(set)
    for value, abc_id in rows:
        index[int(value) if key == 'id' else value].add(abc_id)
    return index


def _by_id(ids) -> Optional[int]:
    return min(ids) if ids else None


def _by_login(ids) -> Optional[int]:
    """Совпадение по логину используется, только если запись одна."""
    return next(iter(ids)) if len(ids) == 1 else None


def _prefetch(ids: Iterable[Optional[int]]) -> list:
    """Загрузить строки пачки одним запросом. Пока список жив, session.get берёт их из памяти без SQL."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return []
    return db.session.scalars(select(AbstractCustomer).where(AbstractCustomer.id.in_(ids))).all()


def _get(abc_id: Optional[int]) -> Optional[AbstractCustomer]:
    return db.session.get(AbstractCustomer, abc_id) if abc_id is not None else None


def _create(**fields) -> AbstractCustomer:
    abc = AbstractCustomer(**fields)
    db.session.add(abc)
    db.session.flush()  # нужен id для индексов и ids_map
    return abc


def _upsert(abc: AbstractCustomer, attr: str, item: dict) -> None:
    """Заменить элемент с тем же id или добавить новый. Если ничего не изменилось — строку не трогаем."""
    items = list(getattr(abc, attr) or [])
    for i, old in enumerate(items):
        if old['general']['id'] == item['general']['id']:
            if old == item:
                return
            items[i] = item
            break
    else:
        items.append(item)
    setattr(abc, attr, items)


def _seen(ids_map: Dict, abc_id: int) -> Dict:
    return ids_map.setdefault(abc_id, {key: set() for _, key in NESTED})


def _merge(target: AbstractCustomer, other: AbstractCustomer,
           by_lead_id: Dict, by_customer_login: Dict, ids_map: Dict) -> None:
    """Слить other в target и удалить other. Индексы и ids_map переводим на target."""
    for attr, _ in NESTED:
        setattr(target, attr, dict_compare_merge(getattr(target, attr) or [], getattr(other, attr) or []))
    for lead in other.amo_leads or []:
        by_lead_id[lead['general']['id']] = {target.id}
    for customer in other.amo_customers or []:
        ids = by_customer_login.get(customer['general'].get('login'))
        if ids is not None:
            ids.discard(other.id)
            ids.add(target.id)
    old = ids_map.pop(other.id, None)
    if old:
        seen = _seen(ids_map, target.id)
        for key in seen:
            seen[key] |= old[key]
    db.session.delete(other)


def lead_action(lead: Lead, by_lead_id: Dict, by_customer_login: Dict, ids_map: Dict) -> AbstractCustomer:
    item = get_lead_json(lead)  # до любых изменений: если данные сделки кривые, падаем здесь
    by_id_row = _get(_by_id(by_lead_id.get(lead.id, ())))
    login = lead.get_field('Логин')
    by_login_row = _get(_by_login(by_customer_login.get(login, ()))) if login else None

    abc = by_id_row or by_login_row
    if by_id_row is not None and by_login_row is not None and by_login_row.id != by_id_row.id:
        _merge(by_id_row, by_login_row, by_lead_id, by_customer_login, ids_map)

    if abc is None:
        abc = _create(amo_leads=[item])
    else:
        _upsert(abc, "amo_leads", item)
    by_lead_id[lead.id] = {abc.id}
    return abc


def _collect_customers(is_full: bool, ids_map: Dict) -> None:
    session = db.session
    by_customer_id = _load_index("amo_customers", "id")
    by_lead_login = _load_index("amo_leads", "login")

    def target(c: Customer) -> Optional[int]:
        return _by_id(by_customer_id.get(c.id, ())) or _by_login(by_lead_login.get(c.get_field('Логин'), ()))

    customers = (c for c in amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
                 if c.get_field('Логин') and not (is_full is False and c.status_id == 111349))
    for batch in _batches(customers):
        loaded = _prefetch(target(c) for c in batch)
        for customer in batch:
            item = get_customer_json(customer)
            abc = _get(target(customer))
            if abc is None:
                abc = _create(amo_customers=[item])
                loaded.append(abc)
            else:
                _upsert(abc, "amo_customers", item)
            by_customer_id[customer.id] = {abc.id}
            if is_full:
                seen = _seen(ids_map, abc.id)
                seen["customers"].add(customer.id)
                seen["contacts"].update(c["id"] for c in customer.contacts)
                seen["companies"].update(c["id"] for c in customer.companies)
        session.commit()


def _collect_leads(is_full: bool, ids_map: Dict) -> bool:
    """Возвращает False, если хотя бы одна сделка не обработалась."""
    session = db.session
    by_lead_id = _load_index("amo_leads", "id")
    by_customer_login = _load_index("amo_customers", "login")
    all_ok = True

    leads = (lead for lead in amocrm_get(Lead, _with=CONTACTS)
             if not (is_full is False and lead.status_id == 143))
    for batch in _batches(leads):
        loaded = _prefetch(
            row_id
            for lead in batch
            for row_id in (_by_id(by_lead_id.get(lead.id, ())),
                           _by_login(by_customer_login.get(lead.get_field('Логин'), ())))
        )
        for amo_lead in batch:
            try:
                abc = lead_action(amo_lead, by_lead_id, by_customer_login, ids_map)
            except Exception as e:
                all_ok = False
                logger.exception(f'{amo_lead} {e}')
                continue
            loaded.append(abc)
            if is_full:
                seen = _seen(ids_map, abc.id)
                seen["leads"].add(amo_lead.id)
                seen["contacts"].update(c["id"] for c in amo_lead.contacts)
        session.commit()
    return all_ok


def _cleanup(ids_map: Dict) -> None:
    """Удалить строки и вложенные сущности, которых не было в выгрузке или с «удалить» в имени.
    Таблица читается пачками по id, а не целиком."""
    session = db.session
    last_id = 0
    while True:
        rows = session.scalars(
            select(AbstractCustomer).where(AbstractCustomer.id > last_id)
            .order_by(AbstractCustomer.id).limit(BATCH_SIZE)
        ).all()
        if not rows:
            break
        last_id = rows[-1].id
        for abc in rows:
            seen = ids_map.get(abc.id)
            if seen is None:
                session.delete(abc)
                continue
            for attr, key in NESTED:
                items = getattr(abc, attr) or []
                kept = [i for i in items
                        if i['general'].get('id') in seen[key]
                        and 'удалить' not in (i['general'].get('name') or '').lower()]
                if len(kept) != len(items):
                    setattr(abc, attr, kept)
            if not abc.amo_leads and not abc.amo_customers:
                session.delete(abc)
        session.commit()


def collect_leads_customers(is_full=True):
    """
    Синхронизация покупателей и сделок из CRM в abstract_customer.
    1. Покупатели: ищем строку по id покупателя, иначе по логину среди сделок (только если строка одна).
    2. Сделки: ищем по id сделки и по логину среди покупателей; две разные строки сливаем в найденную по id.
    3. При полном проходе без ошибок — чистка того, чего не было в выгрузке.
    Поиск идёт по индексам в памяти, строки грузятся и пишутся пачками, commit — раз на пачку.
    """
    ids_map: Dict[int, Dict[str, set]] = {}
    try:
        _collect_customers(is_full, ids_map)
        logger.info(f"Collected customers {is_full=}")
        leads_ok = _collect_leads(is_full, ids_map)
        logger.info(f"Collected leads {is_full=}")
    except Exception:
        db.session.rollback()
        logger.exception("Сбор прерван, чистка не запускается")
        raise

    if is_full is False:
        return
    if not leads_ok:
        logger.error("Часть сделок не обработана, чистка не запускается")
        return
    try:
        logger.info("Чистка покупателей")
        _cleanup(ids_map)
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Чистка: {e}")
