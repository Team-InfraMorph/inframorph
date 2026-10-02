"""Best-effort secret filtering, not a general-purpose DLP detector."""
import re


SECRET_NAME = re.compile(r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|DATABASE_URL|CREDENTIAL)", re.I)
PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s\"'`]+", re.I),
)
ASSIGNMENT = re.compile(
    r"(?i)([\"']?[A-Za-z_][A-Za-z0-9_]*(?:[\"']?)\s*[:=]\s*)([\"'])([^\r\n]*?)\2"
)


class Redactor:
    def __init__(self, known_secrets=()):
        self.secrets = {value for value in known_secrets if isinstance(value, str) and len(value) >= 4}

    def _mask(self, value: str) -> str:
        # Mask short literals in place without replacing single letters throughout
        # unrelated source/schema text on subsequent passes.
        if len(value) >= 4:
            self.secrets.add(value)
        # Preserve source line numbers, including PEM blocks.
        return "[REDACTED]" + "\n" * value.count("\n")

    def clean(self, text: str) -> str:
        for pattern in PATTERNS:
            text = pattern.sub(lambda match: self._mask(match[0]), text)

        def assignment(match):
            value = match[3]
            name = re.split(r"[:=]", match[1], maxsplit=1)[0]
            if SECRET_NAME.search(name) and value and not value.startswith(("${", "[REDACTED]")):
                return match[1] + match[2] + self._mask(value) + match[2]
            return match[0]

        text = ASSIGNMENT.sub(assignment, text)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]" + "\n" * secret.count("\n"))
        return text

    def contains_secret(self, text: str) -> bool:
        return self.clean(text) != text or "[REDACTED]" in text
