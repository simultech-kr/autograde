// Native icon launcher: starts the bundled runtime, stays alive until cleanup,
// and presents errors without opening Terminal or keeping student logs.
#import <Cocoa/Cocoa.h>

@interface AutogradeLauncher : NSObject <NSApplicationDelegate>
@property NSTask *supervisor;
@property NSMutableData *pending;
@property NSString *failure;
@property pid_t editorPID;
@property BOOL stopping;
@property BOOL sessionCleaned;
@end

@implementation AutogradeLauncher
- (void)focusEditor {
    if (self.editorPID > 0) {
        NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:self.editorPID];
        [app activateWithOptions:NSApplicationActivateIgnoringOtherApps];
    }
}
- (void)showFailure:(NSString *)message {
    [NSApp activateIgnoringOtherApps:YES];
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Autograde IDE를 확인해 주세요";
    alert.informativeText = message;
    alert.alertStyle = NSAlertStyleWarning;
    [alert addButtonWithTitle:@"확인"];
    [alert runModal];
}
- (void)consume:(NSData *)data {
    [self.pending appendData:data];
    if (self.pending.length > 16384) { [self.pending setLength:0]; return; }
    const uint8_t newline = '\n';
    NSData *separator = [NSData dataWithBytes:&newline length:1];
    while (YES) {
        NSRange end = [self.pending rangeOfData:separator options:0 range:NSMakeRange(0, self.pending.length)];
        if (end.location == NSNotFound) break;
        NSData *line = [self.pending subdataWithRange:NSMakeRange(0, end.location)];
        [self.pending replaceBytesInRange:NSMakeRange(0, end.location + 1) withBytes:NULL length:0];
        id value = [NSJSONSerialization JSONObjectWithData:line options:0 error:NULL];
        if (![value isKindOfClass:[NSDictionary class]]) continue;
        if ([value[@"event"] isEqual:@"ide-started"] && [value[@"pid"] isKindOfClass:[NSNumber class]]) {
            self.editorPID = [value[@"pid"] intValue];
        }
        if ([value[@"event"] isEqual:@"error"] && [value[@"message"] isKindOfClass:[NSString class]]) {
            self.failure = value[@"message"];
        }
        if ([value[@"event"] isEqual:@"session-cleaned"]) self.sessionCleaned = YES;
    }
}
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    self.pending = [NSMutableData data];
    NSURL *resources = NSBundle.mainBundle.resourceURL;
    NSURL *node = [resources URLByAppendingPathComponent:@"runtime/bin/node"];
    NSURL *script = [resources URLByAppendingPathComponent:@"autograde/mac-launcher.mjs"];
    self.supervisor = [[NSTask alloc] init];
    self.supervisor.executableURL = node;
    self.supervisor.arguments = @[script.path];
    self.supervisor.currentDirectoryURL = resources;
    NSMutableDictionary *environment = [NSProcessInfo.processInfo.environment mutableCopy];
    for (NSString *key in [environment.allKeys copy]) {
        for (NSString *prefix in @[@"NODE_", @"ELECTRON_", @"VSCODE_", @"AUTOGRADE_", @"DYLD_"]) {
            if ([key hasPrefix:prefix]) { [environment removeObjectForKey:key]; break; }
        }
    }
    self.supervisor.environment = environment;
    NSPipe *output = [NSPipe pipe];
    self.supervisor.standardOutput = output;
    self.supervisor.standardError = [NSFileHandle fileHandleWithNullDevice];
    self.supervisor.standardInput = [NSFileHandle fileHandleWithNullDevice];
    __weak AutogradeLauncher *weakSelf = self;
    NSError *error = nil;
    if (![self.supervisor launchAndReturnError:&error]) {
        [self showFailure:@"앱에 포함된 실행 환경을 시작할 수 없습니다. 이 Mac에 맞는 Autograde IDE 앱 전체를 다시 복사해 주세요. Node.js를 별도로 설치할 필요는 없습니다."];
        [NSApp terminate:nil];
        return;
    }
    NSTask *task = self.supervisor;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        // One reader queues messages and completion in order; termination can
        // never overtake the final error or cleanup result from the supervisor.
        while (YES) {
            NSData *data = output.fileHandleForReading.availableData;
            if (!data.length) break;
            dispatch_async(dispatch_get_main_queue(), ^{ [weakSelf consume:data]; });
        }
        [task waitUntilExit];
        dispatch_async(dispatch_get_main_queue(), ^{
            AutogradeLauncher *strongSelf = weakSelf;
            if (task.terminationStatus != 0 || strongSelf.failure || !strongSelf.sessionCleaned) {
                [strongSelf showFailure:strongSelf.failure ?: @"앱 실행 또는 임시 파일 정리를 완료하지 못했습니다. 앱 전체를 다시 설치하거나 담당자에게 이전 실습 상태 확인을 요청해 주세요."];
            }
            if (strongSelf.stopping) [NSApp replyToApplicationShouldTerminate:YES];
            else [NSApp terminate:nil];
        });
    });
}
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    (void)sender; (void)flag; [self focusEditor]; return YES;
}
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    (void)sender;
    if (self.supervisor.running) {
        self.stopping = YES;
        [self.supervisor terminate];
        return NSTerminateLater;
    }
    return NSTerminateNow;
}
@end

int main(int argc, const char *argv[]) {
    (void)argc; (void)argv;
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        AutogradeLauncher *delegate = [[AutogradeLauncher alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
