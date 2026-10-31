"""Role-bound scene-plan chats sharing a browser transport, never a conversation."""
from . import webchat


class SceneChats:
    def __init__(self, transport, sites, log, should_stop=lambda: False):
        self.transport, self.sites = transport, sites
        self.log, self.should_stop = log, should_stop
        self.role_urls = {}
        self.context = {}
        self.last_role = None
        if len(set(sites.values())) == 1 and next(iter(sites.values())) in webchat.REUSE_CHAT:
            raise webchat.WebChatError(
                "This site forces a single reused conversation. Choose different sites for writer and judge.")

    def ask(self, role, text, new_chat, ready=None, continuation=False):
        if self.should_stop():
            raise RuntimeError("Stopped by you.")
        self.last_role = role
        site = self.sites[role]
        saved = self.role_urls.get(role)
        # Judge-first resume has never contacted the writer. Its first message
        # must establish a chat even though the planner requested a correction.
        fresh = bool(new_chat or not saved)
        if continuation and not saved:
            raise webchat.ChatLost("Cannot continue a partial scene plan without its writer conversation.")
        if not fresh:
            self.transport.adopt(site, saved)
        try:
            reply = self.transport.ask(site, text, (), new_chat=fresh, ready=ready)
        except webchat.ChatLost:
            if fresh or continuation:
                raise
            if self.should_stop():
                raise RuntimeError("Stopped by you.")
            # ChatLost means the prompt was not sent. Re-establish exactly once,
            # with full context, rather than sending a short resend instruction
            # into an empty chat. Never retry sign-in/captcha/timeout failures.
            self.log(f"{site}: {role} conversation was lost; opening a fresh role chat")
            context = self.context.get(role, "")
            rebuilt = context + "\n\nLATEST REQUEST:\n" + text if context and context != text else text
            reply = self.transport.ask(site, rebuilt, (), new_chat=True, ready=ready)
        url = self.transport.urls().get(site)
        if not url:
            raise webchat.ChatLost(f"{site}: no conversation URL was recorded for {role}; cannot safely resume")
        if any(other != role and other_url == url for other, other_url in self.role_urls.items()):
            raise webchat.ChatLost("Writer and judge resolved to the same conversation; choose separate chat sites.")
        self.role_urls[role] = url
        if not continuation:
            if fresh or role == "writer" or role not in self.context:
                self.context[role] = text
        return reply
