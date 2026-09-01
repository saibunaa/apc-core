"""Pure fixture-only active-staff validation for future Core composition."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActiveStaffRecord:
    name: str
    role: str


@dataclass(frozen=True, slots=True, init=False)
class ActiveStaffProvider:
    _records: tuple[ActiveStaffRecord, ...]
    _names: frozenset[str]

    def __init__(self, fixture: object):
        if type(fixture) is not tuple or not fixture:
            raise ValueError("active staff fixture is invalid")
        records: list[ActiveStaffRecord] = []
        names: set[str] = set()
        for entry in fixture:
            if type(entry) is not tuple or len(entry) != 2:
                raise ValueError("active staff fixture is invalid")
            name, role = entry
            if type(name) is not str or not name or type(role) is not str or not role or name in names:
                raise ValueError("active staff fixture is invalid")
            names.add(name)
            records.append(ActiveStaffRecord(name=name, role=role))
        records.sort(key=lambda record: record.name)
        object.__setattr__(self, "_records", tuple(records))
        object.__setattr__(self, "_names", frozenset(names))

    def active_staff(self) -> tuple[ActiveStaffRecord, ...]:
        return self._records

    def is_active(self, name: object) -> bool:
        return type(name) is str and name in self._names
