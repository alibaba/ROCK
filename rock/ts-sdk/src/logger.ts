/**
 * Winston-based logger module
 */

import winston from 'winston';
import { envVars } from './env_vars.js';
import { join } from 'path';
import { existsSync, mkdirSync } from 'fs';

/**
 * Log levels
 */
const levels: winston.config.AbstractConfigSetLevels = {
  error: 0,
  warn: 1,
  info: 2,
  http: 3,
  debug: 4,
};

/**
 * Log level colors
 */
const colors: winston.config.AbstractConfigSetColors = {
  error: 'red',
  warn: 'yellow',
  info: 'green',
  http: 'magenta',
  debug: 'cyan',
};

winston.addColors(colors);

/**
 * Custom format for console output
 */
const consoleFormat = winston.format.combine(
  winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss.SSS' }),
  winston.format.colorize({ all: true }),
  winston.format.printf((info) => {
    const { timestamp, level, message, ...meta } = info;
    const metaStr = Object.keys(meta).length ? JSON.stringify(meta) : '';
    return `${timestamp} ${level}: ${message} ${metaStr}`;
  })
);

/**
 * Custom format for file output
 */
const fileFormat = winston.format.combine(
  winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss.SSS' }),
  winston.format.json()
);

/**
 * Get log level from environment
 */
function getLogLevel(): string {
  const level = envVars.ROCK_LOGGING_LEVEL.toLowerCase();
  if (level in levels) {
    return level;
  }
  return 'info';
}

/**
 * Logger cache
 */
const loggerCache = new Map<string, winston.Logger>();

/**
 * Initialize and return a logger instance
 */
export function initLogger(name: string = 'rock', fileName?: string): winston.Logger {
  // Return cached logger if exists
  if (loggerCache.has(name)) {
    return loggerCache.get(name)!;
  }

  const transports: winston.transport[] = [];
  const logLevel = getLogLevel();

  // File transport
  const logPath = envVars.ROCK_LOGGING_PATH;
  const logFileName = fileName ?? envVars.ROCK_LOGGING_FILE_NAME;

  if (logPath) {
    // Ensure directory exists
    if (!existsSync(logPath)) {
      mkdirSync(logPath, { recursive: true });
    }

    transports.push(
      new winston.transports.File({
        filename: join(logPath, logFileName),
        format: fileFormat,
        level: logLevel,
      })
    );
  } else {
    // Console transport
    transports.push(
      new winston.transports.Console({
        format: consoleFormat,
        level: logLevel,
      })
    );
  }

  const logger = winston.createLogger({
    levels,
    defaultMeta: { service: name },
    transports,
  });

  // Cache the logger
  loggerCache.set(name, logger);

  return logger;
}

/**
 * Get or create a child logger
 */
export function getChildLogger(parentName: string, childName: string): winston.Logger {
  const fullName = `${parentName}:${childName}`;
  return initLogger(fullName);
}

/**
 * Create a logger with context
 */
export function createContextLogger(context: string): winston.Logger {
  const logger = initLogger(context);
  return logger;
}

/**
 * Default logger instance
 */
export const defaultLogger = initLogger('rock');

// Re-export winston types
export type Logger = winston.Logger;
export type LogEntry = winston.LogEntry;
