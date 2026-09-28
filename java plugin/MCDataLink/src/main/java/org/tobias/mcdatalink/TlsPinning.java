package org.tobias.mcdatalink;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Locale;

/**
 * TLS that trusts exactly one certificate, given by its SHA-256 fingerprint ("tls-fingerprint" in config.yml).
 * For a self-hosted MCConnect with a self-signed certificate; the socket server logs the fingerprint.
 */
final class TlsPinning {
    private TlsPinning() {
    }

    /** "AB:CD:..." or "abcd..." -> 32 bytes, or null if it is not a SHA-256 fingerprint. */
    static byte[] parse(String fingerprint) {
        String hex = fingerprint.replace(":", "").replace(" ", "").toLowerCase(Locale.ROOT);
        if (!hex.matches("[0-9a-f]{64}")) return null;
        byte[] bytes = new byte[32];
        for (int i = 0; i < 32; i++) bytes[i] = (byte) Integer.parseInt(hex.substring(2 * i, 2 * i + 2), 16);
        return bytes;
    }

    static SSLSocketFactory factory(byte[] fingerprint) throws GeneralSecurityException {
        TrustManager pinned = new X509TrustManager() {
            @Override
            public void checkServerTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                try {
                    byte[] actual = MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded());
                    if (!MessageDigest.isEqual(actual, fingerprint)) {
                        throw new CertificateException("The certificate of MCConnect does not match tls-fingerprint");
                    }
                } catch (java.security.NoSuchAlgorithmException e) {
                    throw new CertificateException(e);
                }
            }

            @Override
            public void checkClientTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                throw new CertificateException("not a server");
            }

            @Override
            public X509Certificate[] getAcceptedIssuers() {
                return new X509Certificate[0];
            }
        };
        SSLContext context = SSLContext.getInstance("TLS");
        context.init(null, new TrustManager[]{pinned}, null);
        return context.getSocketFactory();
    }
}
