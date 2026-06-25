import { z } from 'zod';

/** Harbor environment type — maps to the Harbor CLI ``--environment-type`` option. */
export const EnvironmentType = {
  DOCKER: 'docker',
  DAYTONA: 'daytona',
  E2B: 'e2b',
  MODAL: 'modal',
  RUNLOOP: 'runloop',
  GKE: 'gke',
  ROCK: 'rock',
} as const;

export type EnvironmentType = (typeof EnvironmentType)[keyof typeof EnvironmentType];

/** Zod schema for runtime validation of EnvironmentType values. */
export const EnvironmentTypeSchema = z.nativeEnum(EnvironmentType);
