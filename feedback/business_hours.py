"""Weekly human-support schedules; no presence or response-time guarantees."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

class Interval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(pattern=r'^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$')

    @model_validator(mode='after')
    def ordered(self):
        if self.start >= self.end:
            raise ValueError('End must be after start. Split overnight hours across two days.')
        return self

class WeeklyHours(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    timezone: str = Field(default='UTC', max_length=100)
    # Monday first. Empty day means closed; empty week means no scheduled hours.
    days: list[list[Interval]] = Field(default_factory=lambda: [[] for _ in range(7)], min_length=7, max_length=7)
    collect_email: bool = False

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('Choose an IANA timezone such as America/Chicago') from None
        return value

    @field_validator('days')
    @classmethod
    def windows(cls, days):
        for day in days:
            if len(day)>3: raise ValueError('Use at most three time ranges per day')
            day.sort(key=lambda x:x.start)
            if any(a.end>b.start for a,b in zip(day,day[1:])):
                raise ValueError('Time ranges cannot overlap')
        return days

class HoursEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    mode: Literal['inherit','override','off'] = 'override'
    schedule: WeeklyHours = Field(default_factory=WeeklyHours)

def availability(schedule, source, at=None):
    """Check actual instants against wall-clock windows, including repeated/skipped hours.

    UTC minute iteration is bounded to eight days and only used while closed.
    This avoids inventing nonexistent local times or losing the second DST fold.
    """
    at = at or datetime.now(timezone.utc)
    zone = ZoneInfo(schedule['timezone'])
    def is_open(instant):
        local = instant.astimezone(zone)
        minute = local.strftime('%H:%M')
        return any(w['start'] <= minute < w['end'] for w in schedule['days'][local.weekday()])
    enabled = schedule['enabled']
    opened = is_open(at) if enabled else None
    next_open = None
    if enabled and not opened and any(schedule['days']):
        candidate = at.astimezone(timezone.utc).replace(second=0,microsecond=0)
        for _ in range(8*24*60):
            candidate += timedelta(minutes=1)
            if is_open(candidate):
                next_open = candidate.isoformat().replace('+00:00','Z');break
    return {'enabled':enabled,'source':source,'timezone':schedule['timezone'],'days':schedule['days'],
            'is_open':opened,'next_open_at':next_open,'collect_email':enabled and schedule['collect_email'],
            'observed_at':at.astimezone(timezone.utc).isoformat().replace('+00:00','Z')}


def contact_email(value):
    """Deliberately unverified contact address, never an identity credential."""
    from feedback.errors import Error
    import re
    if value is None: return None
    value=value.strip()
    if not 3<=len(value)<=254 or not re.fullmatch(r'[^\s@<>\x00-\x1f\x7f]+@[^\s@<>\x00-\x1f\x7f]+\.[^\s@<>\x00-\x1f\x7f]+',value):
        raise Error(400,'invalid_email','Enter a valid email address, or leave it blank')
    return value
