function makeNode(spec = {}) {
  const attrs = new Map(Object.entries(spec.attrs ?? {}));
  const src = spec.src ?? '';
  const image = {
    tagName: 'IMG',
    currentSrc: src,
    naturalWidth: spec.width ?? 0,
    naturalHeight: spec.height ?? 0,
    getAttribute: (name) => (name === 'src' ? src : null),
  };
  return {
    tagName: spec.tagName ?? (spec.src !== undefined ? 'IMG' : 'DIV'),
    currentSrc: src,
    naturalWidth: spec.width ?? 0,
    naturalHeight: spec.height ?? 0,
    innerText: spec.text ?? '',
    textContent: spec.text ?? '',
    getAttribute: (name) => {
      if (name === 'src') return src || null;
      if (name === 'data-testid') return spec.testid ?? null;
      if (name === 'id') return spec.id ?? null;
      return attrs.has(name) ? attrs.get(name) : null;
    },
    setAttribute: (name, value) => attrs.set(name, value),
    hasAttribute: (name) => attrs.has(name),
    matches: () => false,
    querySelector: (selector) => {
      if (selector === 'img') return spec.src !== undefined ? image : null;
      return spec.redo === true ? { tagName: 'BUTTON' } : null;
    },
  };
}

function makeLocator(elements, state) {
  const first = () => (elements.length > 0 ? elements[0] : undefined);
  return {
    first: () => makeLocator(elements.slice(0, 1), state),
    nth: (index) => makeLocator(elements[index] ? [elements[index]] : [], state),
    count: async () => elements.length,
    isVisible: async () => first()?.visible ?? false,
    isEnabled: async () => first()?.enabled ?? true,
    click: async (options) => {
      state.calls.push({ method: 'click', options });
      await first()?.onClick?.(state);
    },
    fill: async (value) => {
      state.calls.push({ method: 'fill', value });
      if (first()) first().value = value;
    },
    innerText: async () => first()?.node.innerText ?? '',
    inputValue: async () => first()?.value ?? '',
    getAttribute: async (name) => first()?.node.getAttribute(name) ?? null,
    evaluate: async (fn, arg) => {
      if (!first()) throw new Error('No element to evaluate');
      return fn(first().node, arg);
    },
    filter: ({ hasText }) =>
      makeLocator(elements.filter((element) => (element.node.innerText ?? '').includes(hasText)), state),
    locator: (selector) =>
      makeLocator(
        elements.flatMap((element) =>
          (element.children?.[selector] ?? []).map((child) => ({ node: makeNode(child) })),
        ),
        state,
      ),
    hover: async () => {},
    scrollIntoViewIfNeeded: async () => {},
    setInputFiles: async (files) => {
      state.calls.push({ method: 'setInputFiles', files });
    },
  };
}

export function makePage(initial = {}) {
  const state = {
    url: initial.url ?? 'https://flow.google.com/project/test',
    registry: new Map(),
    requestHandler: initial.requestHandler ?? null,
    pageEvaluate: initial.pageEvaluate,
    events: initial.events ?? {},
    onKey: initial.onKey ?? null,
    calls: [],
    content: '<html></html>',
  };

  function resolve(selector) {
    let visibleOnly = false;
    let key = selector;
    if (key.endsWith(':visible')) {
      visibleOnly = true;
      key = key.slice(0, -':visible'.length);
    }
    let elements = (state.registry.get(key) ?? []).map((spec) => ({
      node: spec.node,
      visible: spec.visible ?? true,
      enabled: spec.enabled ?? true,
      value: spec.value,
      onClick: spec.onClick,
      children: spec.children,
    }));
    if (visibleOnly) elements = elements.filter((element) => element.visible);
    return elements;
  }

  return {
    state,
    calls: state.calls,
    set(selector, specs) {
      state.registry.set(selector, specs.map((spec) => ({ ...spec, node: spec.node ?? makeNode(spec) })));
    },
    locator(selector) {
      return makeLocator(resolve(selector), state);
    },
    url: () => state.url,
    goto: async (url) => {
      state.url = url;
      state.calls.push({ method: 'goto', url });
    },
    reload: async () => {
      state.calls.push({ method: 'reload' });
    },
    keyboard: {
      press: async (key) => {
        state.calls.push({ method: 'press', key });
        state.onKey?.(key);
      },
      type: async (text) => {
        state.calls.push({ method: 'type', text });
      },
    },
    waitForEvent: async (name) => state.events[name] ?? null,
    screenshot: async () => {},
    content: async () => state.content,
    evaluate: async (fn, arg) => (state.pageEvaluate ? state.pageEvaluate(fn, arg) : undefined),
    on: (event) => {
      state.calls.push({ method: 'on', event });
    },
  };
}

export function makeContext(state) {
  return {
    request: {
      get: async (url) => {
        const value = typeof state.requestHandler === 'function' ? await state.requestHandler(url) : null;
        if (!value) return { ok: () => false, body: async () => null };
        return { ok: () => true, body: async () => value };
      },
    },
  };
}
