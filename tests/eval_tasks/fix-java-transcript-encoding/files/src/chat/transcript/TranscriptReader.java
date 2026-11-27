package chat.transcript;

import chat.wire.Frame;
import chat.wire.FrameDecoder;

import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;

public final class TranscriptReader {
    private TranscriptReader() {}

    public static Transcript read(InputStream in) throws IOException {
        byte[] magic = new byte[4];
        int n = in.read(magic);
        if (n != 4 || !Arrays.equals(magic, TranscriptWriter.MAGIC)) {
            throw new IOException("not a transcript");
        }
        FrameDecoder dec = new FrameDecoder(in);
        Transcript t = null;
        Frame f;
        while ((f = dec.read()) != null) {
            switch (f.type()) {
                case HEADER:
                    t = new Transcript(f.body());
                    break;
                case SYSTEM:
                    require(t).event(f.body());
                    break;
                case MESSAGE:
                    Message m = Message.decode(f.body());
                    require(t).say(m.author(), m.text());
                    break;
                case END:
                    return require(t);
                default:
                    throw new IOException("unexpected frame " + f.type());
            }
        }
        throw new IOException("transcript has no END frame");
    }

    private static Transcript require(Transcript t) throws IOException {
        if (t == null) {
            throw new IOException("frame before HEADER");
        }
        return t;
    }

    public static Transcript load(Path path) throws IOException {
        try (InputStream in = new BufferedInputStream(Files.newInputStream(path))) {
            return read(in);
        }
    }
}
