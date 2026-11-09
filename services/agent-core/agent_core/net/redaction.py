"""The redaction check on every outgoing cloud request (MDL-25, PRV-25).

A request to a cloud model may carry only what DAT-18 lets leave the device: no name, contact detail, school name or
device identifier, and no profile id (a per-session pseudonym stands in). The check scans the whole request body for
the session's own identifying terms (the profile's display name, its ids, the device id) and for contact details and
identifiers by pattern; any match blocks the request before it leaves, and the router serves it locally."""

from agent_core.config.personal_data import PATTERNS as PATTERNS
from agent_core.config.personal_data import Finding as Finding
from agent_core.config.personal_data import Redactor as Redactor
