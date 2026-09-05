"""Load the fictitious ODONTO SMART reception catalog idempotently.

This command only loads public reception data from the supplied fictitious
business model.  It does not create patients, clinical records, payments or
inventory movements.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.catalog.models import Promotion, Service  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.organization.models import (  # noqa: E402
    Location,
    Organization,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import AvailabilityRule  # noqa: E402
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID  # noqa: E402


LOCATIONS = (
    {
        "name": "ODONTO SMART Lince",
        "address": "Av. Arequipa 1890, Lince",
        "hours": {"weekday": (time(8), time(20)), "saturday": (time(9), time(18))},
    },
    {
        "name": "ODONTO SMART Jesús María",
        "address": "Av. Brasil 1250, Jesús María",
        "hours": {"weekday": (time(9), time(20)), "saturday": (time(9), time(17))},
    },
    {
        "name": "ODONTO SMART Magdalena",
        "address": "Av. Javier Prado Oeste 650, Magdalena del Mar",
        "hours": {"weekday": (time(8), time(21)), "saturday": (time(9), time(18))},
    },
)


SERVICE_ROWS = (
    ("Consulta odontológica", "60.00", 60, "automatic"),
    ("Evaluación + diagnóstico", "80.00", 60, "automatic"),
    ("Limpieza dental", "120.00", 60, "automatic"),
    ("Limpieza profunda", "220.00", 90, "evaluation_first"),
    ("Curación simple", "120.00", 60, "evaluation_first"),
    ("Curación estética", "180.00", 60, "evaluation_first"),
    ("Blanqueamiento dental", "450.00", 90, "evaluation_first"),
    ("Extracción simple", "180.00", 60, "evaluation_first"),
    ("Extracción compleja", "350.00", 90, "evaluation_first"),
    ("Endodoncia anterior", "550.00", 90, "evaluation_first"),
    ("Endodoncia premolar", "700.00", 90, "evaluation_first"),
    ("Endodoncia molar", "900.00", 120, "evaluation_first"),
    ("Brackets metálicos instalación", "850.00", 120, "evaluation_first"),
    ("Control mensual ortodoncia", "150.00", 30, "automatic"),
    ("Brackets estéticos instalación", "1300.00", 120, "evaluation_first"),
    ("Implante dental", "2800.00", 120, "evaluation_first"),
    ("Corona dental", "1200.00", 90, "evaluation_first"),
    ("Diseño de sonrisa básico", "1800.00", 90, "evaluation_first"),
    ("Carillas de resina por pieza", "350.00", 60, "evaluation_first"),
    ("Carillas cerámicas por pieza", "1100.00", 90, "evaluation_first"),
    ("Profilaxis infantil", "90.00", 45, "automatic"),
    ("Sellante dental infantil", "80.00", 45, "evaluation_first"),
    ("Radiografía periapical", "40.00", 30, "automatic"),
    ("Radiografía panorámica", "90.00", 30, "automatic"),
    ("Prótesis removible", "1000.00", 90, "evaluation_first"),
)


PRACTITIONERS = (
    ("Dra. Andrea Salazar", "ODONTO SMART Lince", "general"),
    ("Dr. Carlos Mendoza", "ODONTO SMART Lince", "ortodoncia"),
    ("Dra. Valeria Ruiz", "ODONTO SMART Jesús María", "endodoncia"),
    ("Dr. Sebastián Torres", "ODONTO SMART Magdalena", "implantologia"),
    ("Dra. Camila Herrera", "ODONTO SMART Jesús María", "odontopediatria"),
)


SPECIALTY_SERVICES = {
    "general": {
        "Limpieza profunda",
        "Curación simple",
        "Curación estética",
        "Blanqueamiento dental",
        "Extracción simple",
        "Diseño de sonrisa básico",
        "Carillas de resina por pieza",
        "Carillas cerámicas por pieza",
    },
    "ortodoncia": {
        "Brackets metálicos instalación",
        "Control mensual ortodoncia",
        "Brackets estéticos instalación",
    },
    "endodoncia": {
        "Endodoncia anterior",
        "Endodoncia premolar",
        "Endodoncia molar",
        "Corona dental",
    },
    "implantologia": {
        "Extracción simple",
        "Extracción compleja",
        "Implante dental",
        "Corona dental",
        "Prótesis removible",
    },
    "odontopediatria": {"Profilaxis infantil", "Sellante dental infantil"},
}


COMMON_SERVICES = {
    "Consulta odontológica",
    "Evaluación + diagnóstico",
    "Limpieza dental",
    "Radiografía periapical",
    "Radiografía panorámica",
}


def _hours_json(hours: dict[str, tuple[time, time]]) -> dict:
    weekday = [{"open": hours["weekday"][0].strftime("%H:%M"), "close": hours["weekday"][1].strftime("%H:%M")}]
    saturday = [{"open": hours["saturday"][0].strftime("%H:%M"), "close": hours["saturday"][1].strftime("%H:%M")}]
    return {
        "monday": weekday,
        "tuesday": weekday,
        "wednesday": weekday,
        "thursday": weekday,
        "friday": weekday,
        "saturday": saturday,
        "sunday": [],
    }


def _one_or_create(session: Session, model, filters: dict, values: dict):
    instance = session.scalar(select(model).filter_by(**filters))
    if instance is None:
        instance = model(**filters, **values)
        session.add(instance)
        session.flush()
    else:
        for field, value in values.items():
            setattr(instance, field, value)
    return instance


def seed_reception_demo(
    session: Session,
    *,
    organization_id: int = BOOTSTRAP_ORGANIZATION_ID,
    promotion_as_of: date | None = None,
) -> dict[str, int]:
    """Upsert all public demo reception data and return deterministic counts."""
    today = promotion_as_of or date.today()
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise ValueError(f"Organization {organization_id} does not exist.")
    organization.name = "ODONTO SMART"

    locations: dict[str, Location] = {}
    for row in LOCATIONS:
        location = _one_or_create(
            session,
            Location,
            {"organization_id": organization_id, "name": row["name"]},
            {
                "timezone": "America/Lima",
                "address": row["address"],
                "public_phone": None,
                "opening_hours": _hours_json(row["hours"]),
                "is_active": True,
            },
        )
        locations[row["name"]] = location

    services: dict[str, Service] = {}
    for name, price, duration, booking_mode in SERVICE_ROWS:
        service = _one_or_create(
            session,
            Service,
            {"organization_id": organization_id, "name": name},
            {
                "duration_minutes": duration,
                "public_description": f"{name}. Precio base referencial sujeto a evaluación odontológica cuando corresponda.",
                "base_price": Decimal(price),
                "currency": "PEN",
                "booking_mode": booking_mode,
                "is_active": True,
            },
        )
        services[name] = service

    promotion_rows = (
        ("PROMO-001", "Sonrisa Smart", "Consulta, evaluación y limpieza para pacientes nuevos. Una promoción por persona y requiere reserva.", "99.00", None, "Limpieza dental", True, 50),
        ("PROMO-002", "Blanqueamiento Smart", "Evaluación, profilaxis básica y blanqueamiento.", "399.00", None, "Blanqueamiento dental", False, 40),
        ("PROMO-003", "Ortodoncia Inicio", "Evaluación, fotografías e instalación de brackets metálicos. Control mensual posterior: S/ 150.", "699.00", None, "Brackets metálicos instalación", False, 30),
        ("PROMO-004", "Referidos Smart", "15% de descuento para el paciente nuevo y S/ 30 de crédito para quien refiere, sujeto a validación humana.", None, "15.00", None, True, 20),
        ("PROMO-005", "Recuperamos tu sonrisa", "Evaluación de reingreso gratuita para pacientes que abandonaron tratamiento por más de 90 días, sujeta a validación humana.", "0.00", None, "Evaluación + diagnóstico", False, 10),
    )
    for code, name, description, price, discount, service_name, new_only, priority in promotion_rows:
        _one_or_create(
            session,
            Promotion,
            {"organization_id": organization_id, "code": code},
            {
                "name": name,
                "description": description,
                "promotional_price": Decimal(price) if price is not None else None,
                "discount_percent": Decimal(discount) if discount is not None else None,
                "currency": "PEN",
                "service_id": services[service_name].id if service_name else None,
                "valid_from": today,
                "valid_until": today + timedelta(days=365),
                "new_patients_only": new_only,
                "priority": priority,
                "is_active": True,
            },
        )

    for display_name, location_name, specialty in PRACTITIONERS:
        practitioner = _one_or_create(
            session,
            Practitioner,
            {"display_name": display_name},
            {"is_active": True},
        )
        _one_or_create(
            session,
            PractitionerMembership,
            {"organization_id": organization_id, "practitioner_id": practitioner.id},
            {"is_active": True},
        )
        location = locations[location_name]
        capability_names = COMMON_SERVICES | SPECIALTY_SERVICES[specialty]
        for service_name in capability_names:
            _one_or_create(
                session,
                PractitionerCapability,
                {
                    "organization_id": organization_id,
                    "practitioner_id": practitioner.id,
                    "service_id": services[service_name].id,
                    "location_id": location.id,
                },
                {"is_active": True},
            )
        location_hours = next(row["hours"] for row in LOCATIONS if row["name"] == location_name)
        for weekday in range(6):
            start_local, end_local = (
                location_hours["weekday"] if weekday < 5 else location_hours["saturday"]
            )
            _one_or_create(
                session,
                AvailabilityRule,
                {
                    "organization_id": organization_id,
                    "practitioner_id": practitioner.id,
                    "location_id": location.id,
                    "day_of_week": weekday,
                    "start_local": start_local,
                    "end_local": end_local,
                },
                {},
            )

    session.flush()
    return {
        "locations": len(LOCATIONS),
        "services": len(SERVICE_ROWS),
        "promotions": len(promotion_rows),
        "practitioners": len(PRACTITIONERS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=int, default=BOOTSTRAP_ORGANIZATION_ID)
    args = parser.parse_args()
    with SessionLocal.begin() as session:
        summary = seed_reception_demo(session, organization_id=args.organization)
    print(
        "Catálogo ficticio de recepción cargado: "
        + ", ".join(f"{key}={value}" for key, value in summary.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

