/**
 * API module barrel exports.
 */

export { localRouter, initLocalApi } from './local.js';
export { proxyRouter, ForwardBackend, ReplayBackend, filterHeaders, type CompletionBackend } from './proxy.js';
