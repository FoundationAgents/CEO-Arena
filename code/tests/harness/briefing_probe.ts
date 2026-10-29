import { readFileSync } from "node:fs";

import { DecisionBriefing } from "../../agent_worker/src/briefing";
import { decodeWorkerInput } from "../../agent_worker/src/protocol";

const message = decodeWorkerInput(readFileSync(0, "utf8"));
if (message.type !== "decide") throw new Error("expected a decision request");
process.stdout.write(new DecisionBriefing(message.request.observation).render());
