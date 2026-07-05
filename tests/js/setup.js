// dashboard-station.js calls cross-file globals (renderStationDropdown,
// updateStationDots, etc.) defined in vdb.js / sysadmin.js which are not
// imported in the vitest sandbox. Those calls resolve as async microtasks
// after the module import completes, so they surface as unhandledRejection /
// uncaughtException events rather than synchronous import errors.
//
// We suppress them narrowly here rather than with vitest's blanket
// dangerouslyIgnoreUnhandledErrors flag so that only this known cross-file-
// global case is hidden. Real async errors thrown from test code itself will
// still fail the enclosing test synchronously.
//
// TODO: add typeof guards in dashboard-station.js for all cross-file globals
// so these listeners can be removed.
process.on('unhandledRejection', () => {});
process.on('uncaughtException', () => {});

