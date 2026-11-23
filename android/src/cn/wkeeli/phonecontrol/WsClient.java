package cn.wkeeli.phonecontrol;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.Locale;

import javax.net.ssl.SSLException;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * 自研最小 RFC6455 WebSocket 客户端（不依赖任何第三方库）。
 *
 * <p>支持：ws/wss 握手、客户端掩码帧、text/binary 收发、ping/pong/close 控制帧、
 * 分片消息重组、应用层心跳（{"t":"ping"}）、断线自动重连（1s→10s 指数退避 + 抖动）。
 *
 * <p>线程模型：内部一个连接/读线程 + 一个心跳线程；所有 Listener 回调都在连接线程上，
 * 调用方自行切线程。send* 可由任意线程调用。
 */
public final class WsClient {

    /** 事件回调，全部在连接线程上触发。 */
    public interface Listener {
        void onOpen();

        void onText(String text);

        void onBinary(byte[] data);

        /** 连接关闭（含服务端 close、对端断开），之后会自动重连。 */
        void onClosed(String reason);

        /** 连接失败（握手失败/网络异常），之后会自动重连。 */
        void onError(String message);
    }

    private static final String WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int MAX_FRAME = 8 * 1024 * 1024;
    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static final int HEARTBEAT_MS = 20000;
    private static final int STALE_MS = 60000;
    private static final int BACKOFF_MAX_MS = 10000;

    private static final int OP_CONTINUATION = 0x0;
    private static final int OP_TEXT = 0x1;
    private static final int OP_BINARY = 0x2;
    private static final int OP_CLOSE = 0x8;
    private static final int OP_PING = 0x9;
    private static final int OP_PONG = 0xA;

    private final String url;
    private final Listener listener;
    private final SecureRandom random = new SecureRandom();
    private final Object writeLock = new Object();

    private volatile boolean stopped;
    private volatile Socket socket;
    private volatile OutputStream out;
    private volatile boolean open;
    private volatile long lastRecvAt;

    private Thread connThread;
    private Thread heartbeatThread;

    public WsClient(String url, Listener listener) {
        this.url = url;
        this.listener = listener;
    }

    /** 启动后台连接线程（立即返回）；随后自动保持连接。 */
    public synchronized void start() {
        if (connThread != null) {
            return;
        }
        stopped = false;
        connThread = new Thread(new Runnable() {
            @Override
            public void run() {
                runLoop();
            }
        }, "ws-conn");
        connThread.setDaemon(true);
        connThread.start();

        heartbeatThread = new Thread(new Runnable() {
            @Override
            public void run() {
                heartbeatLoop();
            }
        }, "ws-heartbeat");
        heartbeatThread.setDaemon(true);
        heartbeatThread.start();
    }

    /** 停止并释放连接；不会再自动重连。 */
    public void stop() {
        stopped = true;
        closeSocket();
        Thread t = connThread;
        if (t != null) {
            t.interrupt();
        }
        Thread h = heartbeatThread;
        if (h != null) {
            h.interrupt();
        }
    }

    public boolean isOpen() {
        return open;
    }

    public void sendText(String text) {
        try {
            writeFrame(OP_TEXT, text.getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            closeSocket();
        }
    }

    public void sendBinary(byte[] data) {
        try {
            writeFrame(OP_BINARY, data);
        } catch (IOException e) {
            closeSocket();
        }
    }

    // ---------------------------------------------------------------- 连接循环

    private void runLoop() {
        int backoff = 1000;
        while (!stopped) {
            try {
                connectAndHandshake();
                open = true;
                lastRecvAt = System.currentTimeMillis();
                backoff = 1000;
                safeOpen();
                readLoop();
                safeClosed("连接已关闭");
            } catch (IOException e) {
                if (!stopped) {
                    safeError("[WS] " + describe(e));
                }
            } catch (RuntimeException e) {
                if (!stopped) {
                    safeError("[WS] " + describe(e));
                }
            } finally {
                open = false;
                closeSocket();
            }
            if (stopped) {
                break;
            }
            int wait = backoff + random.nextInt(400);
            sleepQuietly(wait);
            backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
        }
    }

    private void heartbeatLoop() {
        while (!stopped) {
            sleepQuietly(HEARTBEAT_MS);
            if (stopped) {
                break;
            }
            if (open) {
                // 协议 §3：客户端每 20s 发文本 {"t":"ping"}
                sendText("{\"t\":\"ping\"}");
                if (System.currentTimeMillis() - lastRecvAt > STALE_MS) {
                    // 超过 60s 没收到任何帧，认为连接已死，主动断开触发重连
                    closeSocket();
                }
            }
        }
    }

    private void connectAndHandshake() throws IOException {
        URI uri;
        try {
            uri = new URI(url);
        } catch (Exception e) {
            throw new IOException("URL 无效: " + url);
        }
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        boolean tls;
        if ("wss".equals(scheme)) {
            tls = true;
        } else if ("ws".equals(scheme)) {
            tls = false;
        } else {
            throw new IOException("不支持的协议: " + scheme);
        }
        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            throw new IOException("URL 缺少主机名: " + url);
        }
        int port = uri.getPort();
        if (port <= 0) {
            port = tls ? 443 : 80;
        }
        String path = uri.getRawPath();
        if (path == null || path.isEmpty()) {
            path = "/";
        }
        String query = uri.getRawQuery();
        if (query != null && !query.isEmpty()) {
            path = path + "?" + query;
        }

        Socket s;
        if (tls) {
            SSLSocket ss;
            try {
                ss = (SSLSocket) SSLSocketFactory.getDefault().createSocket(host, port);
            } catch (IOException e) {
                throw new IOException("TLS 连接 " + host + ":" + port + " 失败: " + describe(e));
            }
            // 关键：默认 SSLSocket 只校验证书链、不校验主机名，必须显式开启 HTTPS 式主机名校验，
            // 否则任意有效证书都能中间人劫持这条高权限控制通道。
            SSLParameters parameters = ss.getSSLParameters();
            parameters.setEndpointIdentificationAlgorithm("HTTPS");
            ss.setSSLParameters(parameters);
            ss.setSoTimeout(CONNECT_TIMEOUT_MS);
            try {
                ss.startHandshake();
            } catch (SSLException e) {
                throw new IOException("TLS 证书/主机名校验失败（可能证书过期或被中间人劫持）: " + describe(e));
            } catch (IOException e) {
                throw new IOException("TLS 握手失败: " + describe(e));
            }
            s = ss;
        } else {
            s = new Socket();
            s.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
        }
        s.setTcpNoDelay(true);
        socket = s;

        InputStream in = new BufferedInputStream(s.getInputStream(), 8192);
        OutputStream os = new BufferedOutputStream(s.getOutputStream(), 8192);

        byte[] nonce = new byte[16];
        random.nextBytes(nonce);
        String key = Base64.getEncoder().encodeToString(nonce);
        boolean defaultPort = (tls && port == 443) || (!tls && port == 80);
        String hostHeader = defaultPort ? host : (host + ":" + port);
        String request = "GET " + path + " HTTP/1.1\r\n"
                + "Host: " + hostHeader + "\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + key + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n"
                + "User-Agent: PhoneControl/1.0\r\n"
                + "\r\n";
        os.write(request.getBytes(StandardCharsets.UTF_8));
        os.flush();

        String head = readHttpHead(in);
        String[] lines = head.split("\r\n");
        if (lines.length == 0 || !lines[0].contains(" 101 ")) {
            String first = lines.length > 0 ? lines[0] : "";
            throw new IOException("握手被拒绝: " + first);
        }
        String accept = null;
        for (int i = 1; i < lines.length; i++) {
            int idx = lines[i].indexOf(':');
            if (idx <= 0) {
                continue;
            }
            String name = lines[i].substring(0, idx).trim().toLowerCase(Locale.ROOT);
            String value = lines[i].substring(idx + 1).trim();
            if ("sec-websocket-accept".equals(name)) {
                accept = value;
            }
        }
        String expected = expectedAccept(key);
        if (accept != null && !accept.equals(expected)) {
            throw new IOException("握手校验失败: Sec-WebSocket-Accept 不匹配");
        }

        s.setSoTimeout(0);
        out = os;
        // 握手成功后把 raw InputStream 交给读循环（用 buffered 的，避免丢字节）
        this.in = in;
    }

    private volatile InputStream in;

    private String readHttpHead(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int total = 0;
        while (true) {
            int b = in.read();
            if (b < 0) {
                throw new EOFException("握手响应不完整");
            }
            total++;
            if (total > 16384) {
                throw new IOException("握手响应过长");
            }
            sb.append((char) b);
            int len = sb.length();
            if (len >= 4
                    && sb.charAt(len - 4) == '\r' && sb.charAt(len - 3) == '\n'
                    && sb.charAt(len - 2) == '\r' && sb.charAt(len - 1) == '\n') {
                return sb.substring(0, len - 4);
            }
        }
    }

    private static String expectedAccept(String key) throws IOException {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] digest = md.digest((key + WS_GUID).getBytes(StandardCharsets.UTF_8));
            return Base64.getEncoder().encodeToString(digest);
        } catch (Exception e) {
            throw new IOException("SHA-1 不可用: " + e.getMessage());
        }
    }

    // ---------------------------------------------------------------- 帧读写

    private void writeFrame(int opcode, byte[] payload) throws IOException {
        OutputStream o = out;
        if (o == null) {
            throw new IOException("未连接");
        }
        int len = payload.length;
        byte[] header;
        int headerLen;
        if (len <= 125) {
            header = new byte[6];
            header[0] = (byte) (0x80 | opcode);
            header[1] = (byte) (0x80 | len);
            headerLen = 2;
        } else if (len <= 65535) {
            header = new byte[8];
            header[0] = (byte) (0x80 | opcode);
            header[1] = (byte) (0x80 | 126);
            header[2] = (byte) ((len >>> 8) & 0xFF);
            header[3] = (byte) (len & 0xFF);
            headerLen = 4;
        } else {
            header = new byte[14];
            header[0] = (byte) (0x80 | opcode);
            header[1] = (byte) (0x80 | 127);
            long l = len;
            for (int i = 0; i < 8; i++) {
                header[2 + i] = (byte) ((l >>> (8 * (7 - i))) & 0xFF);
            }
            headerLen = 10;
        }
        byte[] mask = new byte[4];
        random.nextBytes(mask);
        System.arraycopy(mask, 0, header, headerLen, 4);
        byte[] masked = new byte[len];
        for (int i = 0; i < len; i++) {
            masked[i] = (byte) (payload[i] ^ mask[i & 3]);
        }
        synchronized (writeLock) {
            o.write(header);
            o.write(masked);
            o.flush();
        }
    }

    private void readLoop() throws IOException {
        InputStream ins = in;
        if (ins == null) {
            throw new IOException("未连接");
        }
        int fragOpcode = -1;
        ByteArrayOutputStream fragBuffer = null;
        while (!stopped) {
            int b0 = ins.read();
            if (b0 < 0) {
                throw new EOFException("对端关闭连接");
            }
            int b1 = readByte(ins);
            lastRecvAt = System.currentTimeMillis();
            boolean fin = (b0 & 0x80) != 0;
            int opcode = b0 & 0x0F;
            boolean masked = (b1 & 0x80) != 0;
            long len = b1 & 0x7F;
            if (len == 126) {
                len = ((long) readByte(ins) << 8) | readByte(ins);
            } else if (len == 127) {
                len = 0;
                for (int i = 0; i < 8; i++) {
                    len = (len << 8) | readByte(ins);
                }
            }
            if (len < 0 || len > MAX_FRAME) {
                throw new IOException("帧过大: " + len);
            }
            byte[] maskKey = null;
            if (masked) {
                maskKey = new byte[4];
                readFully(ins, maskKey);
            }
            byte[] payload = new byte[(int) len];
            readFully(ins, payload);
            if (maskKey != null) {
                for (int i = 0; i < payload.length; i++) {
                    payload[i] = (byte) (payload[i] ^ maskKey[i & 3]);
                }
            }

            if (opcode == OP_PING) {
                writeFrame(OP_PONG, payload);
                continue;
            }
            if (opcode == OP_PONG) {
                continue;
            }
            if (opcode == OP_CLOSE) {
                int code = 1000;
                String reason = "";
                if (payload.length >= 2) {
                    code = ((payload[0] & 0xFF) << 8) | (payload[1] & 0xFF);
                }
                if (payload.length > 2) {
                    reason = new String(payload, 2, payload.length - 2, StandardCharsets.UTF_8);
                }
                try {
                    writeFrame(OP_CLOSE, new byte[]{(byte) 0x03, (byte) 0xE8});
                } catch (IOException ignored) {
                    // 对端可能已关
                }
                safeClosed("服务端关闭 " + (reason.isEmpty() ? ("code=" + code) : (reason + " (code=" + code + ")")));
                return;
            }
            if (opcode == OP_TEXT || opcode == OP_BINARY) {
                if (!fin) {
                    fragOpcode = opcode;
                    fragBuffer = new ByteArrayOutputStream();
                    fragBuffer.write(payload);
                } else {
                    deliver(opcode, payload);
                }
                continue;
            }
            if (opcode == OP_CONTINUATION) {
                if (fragBuffer == null) {
                    continue;
                }
                fragBuffer.write(payload);
                if (fin) {
                    byte[] whole = fragBuffer.toByteArray();
                    int op = fragOpcode;
                    fragOpcode = -1;
                    fragBuffer = null;
                    deliver(op, whole);
                }
                continue;
            }
            // 未知 opcode 忽略
        }
    }

    private void deliver(int opcode, byte[] payload) {
        try {
            if (opcode == OP_TEXT) {
                listener.onText(new String(payload, StandardCharsets.UTF_8));
            } else {
                listener.onBinary(payload);
            }
        } catch (RuntimeException ignored) {
            // 业务回调异常不影响连接
        }
    }

    private static int readByte(InputStream in) throws IOException {
        int b = in.read();
        if (b < 0) {
            throw new EOFException("连接已断开");
        }
        return b;
    }

    private static void readFully(InputStream in, byte[] buffer) throws IOException {
        int offset = 0;
        while (offset < buffer.length) {
            int n = in.read(buffer, offset, buffer.length - offset);
            if (n < 0) {
                throw new EOFException("连接已断开");
            }
            offset += n;
        }
    }

    // ---------------------------------------------------------------- 辅助

    private void safeOpen() {
        try {
            listener.onOpen();
        } catch (RuntimeException ignored) {
        }
    }

    private void safeClosed(String reason) {
        open = false;
        try {
            listener.onClosed(reason);
        } catch (RuntimeException ignored) {
        }
    }

    private void safeError(String message) {
        try {
            listener.onError(message);
        } catch (RuntimeException ignored) {
        }
    }

    private static String describe(Throwable t) {
        String m = t.getMessage();
        if (m == null || m.isEmpty()) {
            return t.getClass().getSimpleName();
        }
        return m;
    }

    private void closeSocket() {
        Socket s = socket;
        socket = null;
        out = null;
        open = false;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
