/**
 * RemoteUser - Remote user management for sandbox
 */

import { initLogger } from '../logger.js';
import type { CommandResponse } from '../types/responses.js';
import type { AbstractSandbox } from './client.js';

const logger = initLogger('rock.sandbox.user');

/**
 * Abstract remote user interface
 */
export abstract class RemoteUser {
  protected sandbox: AbstractSandbox;
  protected currentUser: string = 'root';

  constructor(sandbox: AbstractSandbox) {
    this.sandbox = sandbox;
  }

  getCurrentUser(): string {
    return this.currentUser;
  }

  abstract createRemoteUser(userName: string): Promise<boolean>;
  abstract isUserExist(userName: string): Promise<boolean>;
}

/**
 * Linux remote user implementation
 */
export class LinuxRemoteUser extends RemoteUser {
  constructor(sandbox: AbstractSandbox) {
    super(sandbox);
  }

  async createRemoteUser(userName: string): Promise<boolean> {
    try {
      if (await this.isUserExist(userName)) {
        return true;
      }

      const response: CommandResponse = await this.sandbox.execute({
        command: ['useradd', '-m', '-s', '/bin/bash', userName],
        timeout: 30,
      });

      logger.info(`user add execute response: ${JSON.stringify(response)}`);

      if (response.exit_code !== 0) {
        return false;
      }

      return true;
    } catch (e) {
      logger.error('create_remote_user failed', e as Error);
      throw e;
    }
  }

  async isUserExist(userName: string): Promise<boolean> {
    try {
      const response: CommandResponse = await this.sandbox.execute({
        command: ['id', userName],
        timeout: 30,
      });

      if (response.exit_code === 0) {
        logger.info(`user ${userName} already exists`);
        return true;
      }
      return false;
    } catch (e) {
      logger.info(`is_user_exist exception: ${e}`);
      throw e;
    }
  }
}
