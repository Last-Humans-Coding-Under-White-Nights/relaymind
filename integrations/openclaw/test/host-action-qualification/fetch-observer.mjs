// Test-only transport observation: attempts are visible even without responses.
export function observeFetch(fetcher, record) {
  return async (url, options) => {
    record({kind:'http_attempt',method:options?.method ?? 'GET',path:new URL(url).pathname});
    const response = await fetcher(url, options);
    record({kind:'http',path:new URL(url).pathname,status:response.status});
    return response;
  };
}
