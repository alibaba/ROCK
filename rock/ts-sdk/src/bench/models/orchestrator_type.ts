import { z } from 'zod';

/** Harbor orchestrator type — controls how trials are scheduled. */
export const OrchestratorType = {
  LOCAL: 'local',
  QUEUE: 'queue',
} as const;

export type OrchestratorType = (typeof OrchestratorType)[keyof typeof OrchestratorType];

/** Zod schema for runtime validation of OrchestratorType values. */
export const OrchestratorTypeSchema = z.nativeEnum(OrchestratorType);
