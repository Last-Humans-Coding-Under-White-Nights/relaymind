// The sanitizer of a host capture (RM12, #135), kept apart from the capture
// script so a test can import it without starting the endpoint.

// Replace the home directory with <home> in a fixture's JSON text. The text is
// JSON, so a home that holds backslashes (C:\Users\x on Windows) appears there
// in its escaped form (C:\\Users\\x); the escaped, the raw, and the
// forward-slash form (C:/Users/x, as Node often prints it) are replaced, the
// escaped one first. A root home (/ in a container run as root, or C:\) names
// no user, and replacing it would rewrite every separator, so it is left.
export function sanitizeCapture(text, home) {
  if (!home || /^([A-Za-z]:)?$/.test(home.replace(/[\\/]+$/, ""))) return text;
  const escaped = JSON.stringify(home).slice(1, -1);
  const forms = [...new Set([escaped, home, home.replace(/\\/g, "/")])];
  let out = text;
  for (const form of forms) out = out.split(form).join("<home>");
  return out;
}
