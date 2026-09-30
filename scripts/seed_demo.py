"""Load the fictitious ODONTO SMART demo state idempotently (B0).

Builds on ``scripts/seed_reception_demo.py`` (organization, 3 locations,
practitioners, services, availability rules) and adds, through the domain
services with the explicit ``system`` execution context:

* 40 fictitious Peruvian patients, each with the lead that books for them;
* confirmed appointments in the past, today and the next two weeks;
* past visits with one service execution and one charge each — paid,
  partially paid and overdue (outstanding balance and at least
  ``OVERDUE_MIN_AGE_DAYS`` old), including one S/ 180 charge 12 days old;
* consumable/resale products whose stock is low in Lince and ample in
  Jesús María (the demo transfers between them), with reorder points that
  make the low one show in ``GET /inventory/low-stock`` (B0.5);
* three ``open`` waitlist entries for "Limpieza dental" (B0.5).

Dates. Every date is an offset from one *anchor* day: ``--anchor-date`` or, by
default, today in America/Lima. Offsets, hours, names and amounts come from
fixed tables and a fixed random seed, so a given anchor always produces the
same state. Rows are found again by natural keys (patient DNI, lead phone, one
appointment per lead, one visit per appointment, one charge per execution,
payment reference, product name, first entry per product/location), so a
second run — even on a later day — creates nothing and never moves the dates
of rows that already exist.

Historical instants (visit start, execution, charge, payment) are owned by the
domain services and default to *now*; the seed backdates them once, right
after creating each row, so the demo has a real history. That backdating and
the appointment→patient link are the only direct ORM writes.

    python scripts/seed_demo.py                         # DATABASE_URL, local only
    python scripts/seed_demo.py --anchor-date 2026-10-01
    python scripts/seed_demo.py --issue-staff-credential # writes .env.demo.local
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.catalog.models import Service  # noqa: E402
from app.clinical.models import Patient, ServiceExecution, Visit  # noqa: E402
from app.clinical.schemas import PatientCreate, ServiceExecutionCreate, VisitCreate  # noqa: E402
from app.clinical.service import (  # noqa: E402
    create_patient,
    create_service_execution,
    create_visit,
)
from app.commercial.models import Lead  # noqa: E402
from app.commercial.schemas import LeadCreate  # noqa: E402
from app.commercial.service import create_lead  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.context import default_context  # noqa: E402
from app.economics.models import Charge, Payment, Product  # noqa: E402
from app.economics.schemas import ChargeCreate, PaymentCreate, PaymentVerify, ProductCreate  # noqa: E402
from app.economics.service import (  # noqa: E402
    create_charge,
    create_payment,
    create_product,
    verify_payment,
)
from app.iam.context import ExecutionContext  # noqa: E402
from app.iam.credentials import issue_credential  # noqa: E402
from app.inventory.models import InventoryMovement  # noqa: E402
from app.inventory.models import ReorderPoint  # noqa: E402
from app.inventory.schemas import EntryCreate, ReorderPointUpsert  # noqa: E402
from app.inventory.service import register_entry, upsert_reorder_point  # noqa: E402
from app.organization.models import Location, Practitioner  # noqa: E402
from app.scheduling.models import Appointment  # noqa: E402
from app.scheduling.service import book_appointment  # noqa: E402
from app.scheduling.waitlist import (  # noqa: E402
    WaitlistEntry,
    WaitlistEntryCreate,
    create_waitlist_entry,
)
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID  # noqa: E402
from scripts.issue_credential import _assign_profile, _resolve_principal  # noqa: E402
from scripts.seed_reception_demo import seed_reception_demo  # noqa: E402

LIMA = ZoneInfo("America/Lima")
RANDOM_SEED = 20260930
PATIENT_COUNT = 40
#: A charge is *overdue* (vencido) when it still has a balance this many days
#: after it was issued. There is no due-date column; the demo defines it here.
OVERDUE_MIN_AGE_DAYS = 7
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
STAFF_PROFILE = "reception-staff-demo"
STAFF_PRINCIPAL_NAME = "reception-staff-demo"
ENV_TOKEN_NAME = "BACKEND_DEMO_TOKEN"
DEFAULT_ENV_FILE = REPO_ROOT / ".env.demo.local"

LINCE = "ODONTO SMART Lince"
JESUS_MARIA = "ODONTO SMART Jesús María"
MAGDALENA = "ODONTO SMART Magdalena"

FIRST_NAMES = (
    ("Lucía", "F"), ("José", "M"), ("María Fernanda", "F"), ("Luis", "M"),
    ("Rosa", "F"), ("Jorge", "M"), ("Carmen", "F"), ("Miguel Ángel", "M"),
    ("Ana Sofía", "F"), ("Renzo", "M"), ("Milagros", "F"), ("Diego", "M"),
    ("Karina", "F"), ("Julio César", "M"), ("Gabriela", "F"), ("Christian", "M"),
    ("Pilar", "F"), ("Alonso", "M"), ("Fiorella", "F"), ("Óscar", "M"),
)
LAST_NAMES = (
    "Quispe", "Flores", "Huamán", "Rojas", "Mamani", "Chávez", "Vargas",
    "Ramírez", "Castillo", "Torres", "Mendoza", "Gutiérrez", "Paredes",
    "Salazar", "Ccori", "Espinoza", "Villanueva", "Cárdenas", "Palomino", "Ticona",
)
ACQUISITION_SOURCES = ("direct", "referral", "promotion")

#: Services every practitioner offers at their own location, all ≤ 60 minutes.
COMMON_DEMO_SERVICES = (
    "Consulta odontológica",
    "Evaluación + diagnóstico",
    "Limpieza dental",
    "Radiografía periapical",
    "Radiografía panorámica",
)
PRACTITIONER_NAMES = (
    "Dra. Andrea Salazar",
    "Dr. Carlos Mendoza",
    "Dra. Valeria Ruiz",
    "Dr. Sebastián Torres",
    "Dra. Camila Herrera",
)


@dataclass(frozen=True)
class PastVisit:
    days_ago: int
    practitioner: str
    service: str
    #: ``paid`` · ``partial`` · ``unpaid``
    payment: str
    method: str = "efectivo"
    partial_amount: Decimal | None = None
    verified: bool = True


#: Hand-written rows the demo script talks about; the rest are generated.
KEY_PAST_VISITS = (
    PastVisit(12, "Dra. Andrea Salazar", "Extracción simple", "unpaid"),
    PastVisit(20, "Dr. Sebastián Torres", "Evaluación + diagnóstico", "partial",
              method="yape", partial_amount=Decimal("30.00")),
    PastVisit(9, "Dra. Valeria Ruiz", "Limpieza dental", "unpaid"),
    PastVisit(15, "Dr. Carlos Mendoza", "Control mensual ortodoncia", "partial",
              partial_amount=Decimal("50.00")),
    PastVisit(3, "Dra. Camila Herrera", "Profilaxis infantil", "unpaid"),
    PastVisit(2, "Dra. Andrea Salazar", "Consulta odontológica", "partial",
              method="plin", partial_amount=Decimal("20.00"), verified=False),
)
GENERATED_PAST_VISITS = 18
FUTURE_APPOINTMENTS = 12
TODAY_APPOINTMENTS = 4

PRODUCTS = (
    # name, unit, kind, stock per location
    ("Anestesia lidocaína 2% (cartucho)", "cartucho", "consumible",
     {LINCE: "4", JESUS_MARIA: "120", MAGDALENA: "30"}),
    ("Guantes de nitrilo talla M (caja x100)", "caja", "consumible",
     {LINCE: "18", JESUS_MARIA: "25", MAGDALENA: "12"}),
    ("Resina compuesta A2 (jeringa 4 g)", "jeringa", "consumible",
     {LINCE: "15", JESUS_MARIA: "9", MAGDALENA: "11"}),
    ("Mascarilla quirúrgica (caja x50)", "caja", "consumible",
     {LINCE: "22", JESUS_MARIA: "30", MAGDALENA: "16"}),
    ("Cepillo dental suave", "unidad", "reventa",
     {LINCE: "60", JESUS_MARIA: "45", MAGDALENA: "38"}),
    ("Hilo dental 50 m", "unidad", "reventa",
     {LINCE: "40", JESUS_MARIA: "3", MAGDALENA: "25"}),
)
LOW_STOCK_PRODUCT = PRODUCTS[0][0]
#: Reorder minimum per location (B0.5). Only ``LOW_STOCK_PRODUCT`` gets one:
#: 4 < 10 in Lince (low), 120 and 30 elsewhere (ample).
REORDER_POINTS = ((LOW_STOCK_PRODUCT, (LINCE, JESUS_MARIA, MAGDALENA), "10"),)
#: Patient indexes (future appointments) that also wait for an earlier slot.
WAITLIST_ROWS = (
    (30, LINCE, "morning"),
    (31, JESUS_MARIA, "afternoon"),
    (32, MAGDALENA, "any"),
)
WAITLIST_SERVICE = "Limpieza dental"


def default_anchor() -> date:
    """Today in the clinic's timezone."""
    return datetime.now(LIMA).date()


def assert_local_database_url(url: str, *, allow_remote: bool = False) -> None:
    """Refuse any database that is not on this machine unless explicitly allowed.

    ``.env.local`` has held a hosted pooler URL before a local one; a demo seed
    must never land there by accident. The message names the host only.
    """
    host = make_url(url).host
    if host is None or host in LOCAL_HOSTS or allow_remote:
        return
    raise SystemExit(
        f"seed_demo: el host de la base de datos no es local ({host}). "
        "Usa --allow-remote-database solo si es intencional."
    )


def _idle(session: Session) -> Session:
    """Domain services own their transaction; hand them an idle session."""
    if session.in_transaction():
        session.commit()
    return session


def _patient_rows() -> list[dict]:
    rows = []
    for index in range(PATIENT_COUNT):
        first, sexo = FIRST_NAMES[index % len(FIRST_NAMES)]
        paternal = LAST_NAMES[index % len(LAST_NAMES)]
        maternal = LAST_NAMES[(index * 7 + 3) % len(LAST_NAMES)]
        rows.append(
            {
                "full_name": f"{first} {paternal} {maternal}",
                "dni": f"{70_100_000 + index * 4_231:08d}",
                "sexo": sexo,
                "phone": f"+5199{index:07d}",
                "birth_date": date(1965 + (index * 3) % 45, 1 + index % 12, 1 + (index * 5) % 28),
            }
        )
    return rows


def _past_plan(rng: random.Random) -> list[PastVisit]:
    plan = list(KEY_PAST_VISITS)
    for _ in range(GENERATED_PAST_VISITS):
        roll = rng.random()
        method = rng.choice(("efectivo", "tarjeta", "yape", "plin", "transferencia"))
        plan.append(
            PastVisit(
                days_ago=rng.randint(1, 45),
                practitioner=rng.choice(PRACTITIONER_NAMES),
                service=rng.choice(COMMON_DEMO_SERVICES),
                payment="paid" if roll < 0.85 else "partial",
                method=method,
                partial_amount=Decimal("20.00") if roll >= 0.85 else None,
                verified=method in ("efectivo", "tarjeta") or rng.random() < 0.7,
            )
        )
    return plan


class _Slots:
    """Deterministic, collision-free local slots per practitioner and day."""

    HOURS = (9, 10, 11, 12, 14, 15)

    def __init__(self) -> None:
        self.used: set[tuple[str, date, int]] = set()

    def take(self, practitioner: str, day: date, *, backwards: bool) -> datetime:
        while True:
            if day.weekday() == 6:  # no Sunday availability rules
                day = day - timedelta(days=1) if backwards else day + timedelta(days=1)
                continue
            for hour in self.HOURS:
                key = (practitioner, day, hour)
                if key not in self.used:
                    self.used.add(key)
                    return datetime.combine(day, time(hour), tzinfo=LIMA).astimezone(timezone.utc)
            day = day - timedelta(days=1) if backwards else day + timedelta(days=1)


def _ensure_patient(session: Session, ctx: ExecutionContext, row: dict) -> Patient:
    patient = session.scalar(
        select(Patient).where(
            Patient.organization_id == ctx.organization_id, Patient.dni == row["dni"]
        )
    )
    if patient is None:
        patient = create_patient(_idle(session), PatientCreate(**row), ctx=ctx)
    return patient


def _ensure_lead(session: Session, ctx: ExecutionContext, row: dict, index: int) -> Lead:
    lead = session.scalar(
        select(Lead).where(
            Lead.organization_id == ctx.organization_id,
            Lead.contact_phone == row["phone"],
        )
    )
    if lead is None:
        lead = create_lead(
            _idle(session),
            LeadCreate(
                full_name=row["full_name"],
                contact_phone=row["phone"],
                acquisition_source=ACQUISITION_SOURCES[index % len(ACQUISITION_SOURCES)],
            ),
            ctx=ctx,
        )
    return lead


def _ensure_appointment(
    session: Session,
    ctx: ExecutionContext,
    *,
    lead: Lead,
    patient: Patient,
    service: Service,
    location: Location,
    practitioner: Practitioner,
    start: datetime,
) -> Appointment:
    appointment = session.scalar(
        select(Appointment)
        .where(
            Appointment.organization_id == ctx.organization_id,
            Appointment.lead_id == lead.id,
        )
        .order_by(Appointment.id)
        .limit(1)
    )
    if appointment is None:
        appointment = book_appointment(
            _idle(session),
            ctx=ctx,
            lead_id=lead.id,
            service_id=service.id,
            location_id=location.id,
            practitioner_id=practitioner.id,
            start=start,
        )
        # The patient link is normally set by the contact-proposal flow; the
        # demo agenda shows who the appointment is for.
        appointment = session.get(Appointment, appointment.id)
        appointment.patient_id = patient.id
        session.commit()
    return appointment


def _charge_past_visit(
    session: Session,
    ctx: ExecutionContext,
    *,
    appointment: Appointment,
    patient: Patient,
    service: Service,
    visit_plan: PastVisit,
    payment_reference: str,
    issued_on: date,
) -> None:
    org_id = ctx.organization_id
    started = appointment.start_utc
    visit = session.scalar(
        select(Visit).where(Visit.organization_id == org_id, Visit.appointment_id == appointment.id)
    )
    if visit is None:
        visit = create_visit(
            _idle(session),
            VisitCreate(patient_id=patient.id, appointment_id=appointment.id),
            ctx=ctx,
        )
        visit = session.get(Visit, visit.id)
        visit.started_at = started
        visit.created_at = started
        session.commit()

    execution = session.scalar(
        select(ServiceExecution).where(
            ServiceExecution.organization_id == org_id,
            ServiceExecution.visit_id == visit.id,
            ServiceExecution.service_id == service.id,
        )
    )
    if execution is None:
        execution = create_service_execution(
            _idle(session),
            visit.id,
            ServiceExecutionCreate(service_id=service.id, executed_price=service.base_price),
            ctx=ctx,
        )
        execution = session.get(ServiceExecution, execution.id)
        execution.executed_at = started + timedelta(minutes=20)
        session.commit()

    # The charge is issued on ``anchor - days_ago`` at 18:00 Lima, whatever
    # weekday that is: a Sunday visit moves back to Saturday (no Sunday rules)
    # but its charge keeps the planned age, so "S/ 180, 12 days" always holds.
    finished = max(
        started + timedelta(minutes=service.duration_minutes),
        datetime.combine(issued_on, time(18), tzinfo=LIMA).astimezone(timezone.utc),
    )
    charge = session.scalar(
        select(Charge).where(
            Charge.organization_id == org_id, Charge.service_execution_id == execution.id
        )
    )
    if charge is None:
        charge = create_charge(_idle(session), execution.id, ChargeCreate(), ctx=ctx)
        charge = session.get(Charge, charge.id)
        charge.created_at = finished
        session.commit()

    if visit_plan.payment == "unpaid":
        return
    amount = charge.amount if visit_plan.payment == "paid" else visit_plan.partial_amount
    payment = session.scalar(
        select(Payment).where(
            Payment.organization_id == org_id,
            Payment.charge_id == charge.id,
            Payment.reference == payment_reference,
        )
    )
    if payment is None:
        payment = create_payment(
            _idle(session),
            charge.id,
            PaymentCreate(amount=amount, method=visit_plan.method, reference=payment_reference),
            ctx=ctx,
        )
        payment = session.get(Payment, payment.id)
        payment.paid_at = finished
        session.commit()
        if visit_plan.verified:
            verify_payment(_idle(session), payment.id, PaymentVerify(), ctx=ctx)
            payment = session.get(Payment, payment.id)
            payment.verified_at = finished + timedelta(hours=2)
            session.commit()


def _seed_inventory(session: Session, ctx: ExecutionContext, locations: dict[str, Location]) -> None:
    org_id = ctx.organization_id
    for name, unit, kind, stock in PRODUCTS:
        product = session.scalar(
            select(Product).where(Product.organization_id == org_id, Product.name == name)
        )
        if product is None:
            product = create_product(
                _idle(session), ProductCreate(name=name, unit=unit, kind=kind), ctx=ctx
            )
        for location_name, quantity in stock.items():
            location = locations[location_name]
            has_movement = session.scalar(
                select(func.count())
                .select_from(InventoryMovement)
                .where(
                    InventoryMovement.organization_id == org_id,
                    InventoryMovement.product_id == product.id,
                    InventoryMovement.location_id == location.id,
                )
            )
            if not has_movement:
                register_entry(
                    _idle(session),
                    product.id,
                    EntryCreate(location_id=location.id, quantity=Decimal(quantity)),
                    ctx=ctx,
                )


def _seed_reorder_points(
    session: Session, ctx: ExecutionContext, locations: dict[str, Location]
) -> None:
    org_id = ctx.organization_id
    for product_name, location_names, minimum in REORDER_POINTS:
        product = session.scalar(
            select(Product).where(Product.organization_id == org_id, Product.name == product_name)
        )
        for location_name in location_names:
            location = locations[location_name]
            exists = session.scalar(
                select(ReorderPoint.id).where(
                    ReorderPoint.organization_id == org_id,
                    ReorderPoint.product_id == product.id,
                    ReorderPoint.location_id == location.id,
                )
            )
            if exists is None:
                upsert_reorder_point(
                    _idle(session),
                    product.id,
                    location.id,
                    ReorderPointUpsert(min_quantity=Decimal(minimum)),
                    ctx=ctx,
                )


def _seed_waitlist(
    session: Session,
    ctx: ExecutionContext,
    *,
    anchor: date,
    locations: dict[str, Location],
    services: dict[str, Service],
    patients: list[dict],
) -> None:
    org_id = ctx.organization_id
    service = services[WAITLIST_SERVICE]
    for index, location_name, window in WAITLIST_ROWS:
        row = patients[index]
        lead = session.scalar(
            select(Lead).where(Lead.organization_id == org_id, Lead.contact_phone == row["phone"])
        )
        patient = session.scalar(
            select(Patient).where(Patient.organization_id == org_id, Patient.dni == row["dni"])
        )
        exists = session.scalar(
            select(WaitlistEntry.id).where(
                WaitlistEntry.organization_id == org_id,
                WaitlistEntry.lead_id == lead.id,
                WaitlistEntry.service_id == service.id,
                WaitlistEntry.status == "open",
            )
        )
        if exists is None:
            create_waitlist_entry(
                _idle(session),
                WaitlistEntryCreate(
                    lead_id=lead.id,
                    patient_id=patient.id,
                    service_id=service.id,
                    location_id=locations[location_name].id,
                    earliest_date=anchor + timedelta(days=1),
                    latest_date=anchor + timedelta(days=14),
                    preferred_window=window,
                    notes="Quiere adelantar su cita si se libera un horario.",
                ),
                ctx=ctx,
            )


def _summary(session: Session, organization_id: int, anchor: date) -> dict[str, int]:
    def count(model) -> int:
        return session.scalar(
            select(func.count()).select_from(model).where(model.organization_id == organization_id)
        )

    overdue = 0
    for charge in session.scalars(select(Charge).where(Charge.organization_id == organization_id)):
        paid = session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.charge_id == charge.id)
        )
        age = (anchor - charge.created_at.astimezone(LIMA).date()).days
        if paid < charge.amount and age >= OVERDUE_MIN_AGE_DAYS:
            overdue += 1
    summary = {
        "patients": count(Patient),
        "appointments": count(Appointment),
        "visits": count(Visit),
        "charges": count(Charge),
        "payments": count(Payment),
        "overdue_charges": overdue,
        "products": count(Product),
    }
    session.commit()
    return summary


def seed_demo(
    session: Session,
    *,
    organization_id: int = BOOTSTRAP_ORGANIZATION_ID,
    anchor: date | None = None,
) -> dict[str, int]:
    """Upsert the whole demo state and return deterministic counts."""
    anchor = anchor or default_anchor()
    _idle(session)
    seed_reception_demo(session, organization_id=organization_id, promotion_as_of=anchor)
    session.commit()

    ctx = default_context(organization_id)
    locations = {
        row.name: row
        for row in session.scalars(select(Location).where(Location.organization_id == organization_id))
    }
    services = {
        row.name: row
        for row in session.scalars(select(Service).where(Service.organization_id == organization_id))
    }
    practitioners = {
        row.display_name: row
        for row in session.scalars(
            select(Practitioner).where(Practitioner.display_name.in_(PRACTITIONER_NAMES))
        )
    }
    from scripts.seed_reception_demo import PRACTITIONERS

    home = {name: location for name, location, _specialty in PRACTITIONERS}
    session.commit()

    rng = random.Random(RANDOM_SEED)
    slots = _Slots()
    patients = _patient_rows()
    past = _past_plan(rng)

    for index, row in enumerate(patients):
        patient = _ensure_patient(session, ctx, row)
        lead = _ensure_lead(session, ctx, row, index)
        if index < len(past):
            plan = past[index]
            start = slots.take(plan.practitioner, anchor - timedelta(days=plan.days_ago), backwards=True)
            practitioner_name, service_name = plan.practitioner, plan.service
        else:
            offset = index - len(past)
            practitioner_name = PRACTITIONER_NAMES[offset % len(PRACTITIONER_NAMES)]
            service_name = COMMON_DEMO_SERVICES[offset % len(COMMON_DEMO_SERVICES)]
            if offset < TODAY_APPOINTMENTS:
                start = slots.take(practitioner_name, anchor, backwards=False)
            else:
                days_ahead = 1 + (offset * 5) % 14
                start = slots.take(
                    practitioner_name, anchor + timedelta(days=days_ahead), backwards=False
                )
            plan = None
        appointment = _ensure_appointment(
            session,
            ctx,
            lead=lead,
            patient=patient,
            service=services[service_name],
            location=locations[home[practitioner_name]],
            practitioner=practitioners[practitioner_name],
            start=start,
        )
        if plan is not None:
            _charge_past_visit(
                session,
                ctx,
                appointment=appointment,
                patient=patient,
                service=services[service_name],
                visit_plan=plan,
                payment_reference=f"DEMO-{index + 1:03d}",
                issued_on=anchor - timedelta(days=plan.days_ago),
            )
        session.commit()

    _seed_inventory(session, ctx, locations)
    _seed_reorder_points(session, ctx, locations)
    _seed_waitlist(
        session, ctx, anchor=anchor, locations=locations, services=services, patients=patients
    )
    session.commit()
    return _summary(session, organization_id, anchor)


def issue_staff_credential(
    session: Session,
    *,
    organization_id: int = BOOTSTRAP_ORGANIZATION_ID,
    env_path: Path = DEFAULT_ENV_FILE,
) -> Path:
    """Mint a ``reception-staff-demo`` credential and write it to ``env_path``.

    The token is never printed: it goes only into a 0600 file that
    ``.gitignore`` already covers (``.env*.local``).
    """
    _idle(session)
    principal = _resolve_principal(
        session,
        organization_id=organization_id,
        name=STAFF_PRINCIPAL_NAME,
        principal_type="integration",
    )
    _assign_profile(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        profile=STAFF_PROFILE,
    )
    _credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=STAFF_PRINCIPAL_NAME,
    )
    session.commit()

    env_path = Path(env_path)
    descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{ENV_TOKEN_NAME}={token}\n")
    os.chmod(env_path, 0o600)
    print(f"{ENV_TOKEN_NAME} escrito en {env_path.name}")
    return env_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=None, help="Por defecto DATABASE_URL.")
    parser.add_argument(
        "--allow-remote-database",
        action="store_true",
        help="Permite un host no local (nunca por defecto).",
    )
    parser.add_argument("--organization", type=int, default=BOOTSTRAP_ORGANIZATION_ID)
    parser.add_argument(
        "--anchor-date",
        type=date.fromisoformat,
        default=None,
        help="Día ancla YYYY-MM-DD (por defecto hoy en America/Lima).",
    )
    parser.add_argument(
        "--issue-staff-credential",
        action="store_true",
        help=f"Emite una credencial {STAFF_PROFILE} y la escribe en .env.demo.local.",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url = args.database_url or get_settings().database_url
    assert_local_database_url(url, allow_remote=args.allow_remote_database)

    engine = create_engine(url, pool_pre_ping=True)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with maker() as session:
            summary = seed_demo(
                session, organization_id=args.organization, anchor=args.anchor_date
            )
            print(
                "Demo ODONTO SMART cargada: "
                + ", ".join(f"{key}={value}" for key, value in summary.items())
            )
            if args.issue_staff_credential:
                issue_staff_credential(
                    session, organization_id=args.organization, env_path=args.env_file
                )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
