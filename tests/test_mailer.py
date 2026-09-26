import smtplib

from database import SMTPMailer as mailer_module
from database.SMTPMailer import SMTPMailer


class FakeSMTP:
    instances = []
    fail_next_send = False

    def __init__(self, host, port, timeout=None):
        self.sent = []
        FakeSMTP.instances.append(self)

    def starttls(self):
        pass

    def login(self, user, password):
        self.user = user

    def sendmail(self, sender, recipient, message):
        if FakeSMTP.fail_next_send:
            FakeSMTP.fail_next_send = False
            raise smtplib.SMTPServerDisconnected("dropped")
        self.sent.append((sender, recipient, message))

    def quit(self):
        pass


def make_mailer(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(SMTPMailer, "SEND_INTERVAL", 0)
    return SMTPMailer("smtp.example.com", 587, "noreply@example.com", "pw")


def test_sends_queued_mails_and_stops(monkeypatch):
    mailer = make_mailer(monkeypatch)
    mailer.send_email("a@example.com", "Hi", "<p>1</p>")
    mailer.send_email("b@example.com", "Hi", "<p>2</p>")
    mailer.stop()
    sent = [mail for smtp in FakeSMTP.instances for mail in smtp.sent]
    assert [recipient for _, recipient, _ in sent] == ["a@example.com", "b@example.com"]
    assert "Message-ID:" in sent[0][2] and "Date:" in sent[0][2]
    assert not mailer.worker_thread.is_alive()


def test_reconnects_when_connection_dropped(monkeypatch):
    mailer = make_mailer(monkeypatch)
    FakeSMTP.fail_next_send = True
    mailer.send_email("a@example.com", "Hi", "<p>1</p>")
    mailer.stop()
    assert len(FakeSMTP.instances) == 2
    assert FakeSMTP.instances[1].sent[0][1] == "a@example.com"
