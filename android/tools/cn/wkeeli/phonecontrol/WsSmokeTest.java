package cn.wkeeli.phonecontrol;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 纯 JDK 的 WsClient 冒烟自测（不依赖 Android，也不进 APK）。
 *
 * <p>连接一个 WebSocket 服务端，验证：握手成功、文本帧收发、二进制帧发送、
 * 服务端主动文本消息解析、应用层 ping/pong。默认连本地 ws_echo_server.py。
 *
 * <p>用法：java -cp build/wstest cn.wkeeli.phonecontrol.WsSmokeTest "ws://127.0.0.1:8099/agent/ws/device?key=test"
 */
public final class WsSmokeTest {

    private WsSmokeTest() {
    }

    public static void main(String[] args) throws Exception {
        String url = args.length > 0 ? args[0] : "ws://127.0.0.1:8099/agent/ws/device?key=test";
        final CountDownLatch openLatch = new CountDownLatch(1);
        final CountDownLatch viewersLatch = new CountDownLatch(1);
        final CountDownLatch pongLatch = new CountDownLatch(1);
        final AtomicBoolean opened = new AtomicBoolean(false);
        final AtomicBoolean binaryOk = new AtomicBoolean(false);

        WsClient client = new WsClient(url, new WsClient.Listener() {
            @Override
            public void onOpen() {
                opened.set(true);
                System.out.println("OPEN " + url);
                openLatch.countDown();
            }

            @Override
            public void onText(String text) {
                System.out.println("TEXT " + text);
                if (text != null && text.contains("\"viewers\"")) {
                    viewersLatch.countDown();
                }
                if (text != null && text.contains("\"pong\"")) {
                    pongLatch.countDown();
                }
            }

            @Override
            public void onBinary(byte[] data) {
                System.out.println("BINARY " + data.length);
            }

            @Override
            public void onClosed(String reason) {
                System.out.println("CLOSED " + reason);
            }

            @Override
            public void onError(String message) {
                System.out.println("ERROR " + message);
            }
        });

        client.start();
        boolean ok = openLatch.await(15, TimeUnit.SECONDS);
        if (!ok) {
            System.out.println("RESULT FAIL: handshake timeout (15s)");
            client.stop();
            System.exit(1);
        }

        // 模拟 App 的行为：hello / 应用层心跳 / 二进制 JPEG 帧
        client.sendText("{\"t\":\"hello\",\"name\":\"ws-smoke\",\"w\":1080,\"h\":2400,"
                + "\"accessibility\":false,\"version\":1}");
        client.sendText("{\"t\":\"ping\"}");
        byte[] fakeJpeg = new byte[]{(byte) 0xFF, (byte) 0xD8, (byte) 0xFF, (byte) 0xE0,
                0, 0x10, 'J', 'F', 'I', 'F', 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, (byte) 0xFF, (byte) 0xD9};
        client.sendBinary(fakeJpeg);
        binaryOk.set(client.isOpen());

        boolean gotViewers = viewersLatch.await(8, TimeUnit.SECONDS);
        boolean gotPong = pongLatch.await(8, TimeUnit.SECONDS);

        // 断开测试：stop() 之后不应再回调 onOpen
        client.stop();
        Thread.sleep(600);
        boolean closedCleanly = !client.isOpen();

        ok = opened.get() && gotViewers && gotPong && binaryOk.get() && closedCleanly;
        System.out.println("RESULT " + (ok ? "PASS" : "FAIL")
                + " opened=" + opened.get()
                + " viewers=" + gotViewers
                + " pong=" + gotPong
                + " binarySent=" + binaryOk.get()
                + " closedCleanly=" + closedCleanly);
        System.exit(ok ? 0 : 1);
    }
}
