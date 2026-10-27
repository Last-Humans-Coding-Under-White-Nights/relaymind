// Remove the previous build so dist/ holds only what the current sources emit.
import { rmSync } from "node:fs";

rmSync(new URL("../dist/", import.meta.url), { recursive: true, force: true });
