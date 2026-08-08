package org.confidentia.crypto;

import org.apache.hadoop.conf.Configuration;
import org.apache.parquet.crypto.KeyAccessDeniedException;
import org.apache.parquet.crypto.ParquetCryptoRuntimeException;
import org.apache.parquet.crypto.keytools.KmsClient;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;

/**
 * A local Key Management Service for Parquet Modular Encryption.
 *
 * <p>Parquet ships a mock KMS only in its <em>test</em> artifact, which is not on the
 * Spark runtime classpath. Rather than pull in a test jar, CONFIDENTIA implements the
 * {@link KmsClient} interface directly. This is not throwaway spike code: this class is
 * the seam where the Key Broker Service will later be attached. At that point
 * {@link #wrapKey} and {@link #unwrapKey} stop reading master keys from Hadoop
 * configuration and instead present an attestation report to the broker, which releases
 * the master key only if the enclave measurement satisfies policy (contribution C2).
 *
 * <p>Master keys are supplied through {@code parquet.encryption.key.list} as
 * {@code keyId:base64Key} pairs. Key wrapping is AES-256-GCM: the wrapped form is
 * {@code base64(nonce || ciphertext || tag)}. The master key identifier is bound in as
 * additional authenticated data, so a wrapped key cannot be silently transplanted from
 * one key identifier to another.
 */
public class LocalKmsClient implements KmsClient {

    public static final String KEY_LIST_PROPERTY = "parquet.encryption.key.list";

    private static final int GCM_NONCE_BYTES = 12;
    private static final int GCM_TAG_BITS = 128;

    private final Map<String, byte[]> masterKeys = new HashMap<>();
    private final SecureRandom random = new SecureRandom();

    @Override
    public void initialize(
            Configuration configuration,
            String kmsInstanceID,
            String kmsInstanceURL,
            String accessToken) throws KeyAccessDeniedException {

        String[] keyList = configuration.getTrimmedStrings(KEY_LIST_PROPERTY);
        if (keyList == null || keyList.length == 0) {
            throw new ParquetCryptoRuntimeException(
                    "No master keys supplied. Set " + KEY_LIST_PROPERTY
                            + " to a comma-separated list of keyId:base64Key pairs.");
        }
        for (String entry : keyList) {
            int separator = entry.indexOf(':');
            if (separator < 0) {
                throw new ParquetCryptoRuntimeException(
                        "Malformed key list entry (expected keyId:base64Key): " + entry);
            }
            String keyId = entry.substring(0, separator).trim();
            byte[] keyBytes = Base64.getDecoder().decode(entry.substring(separator + 1).trim());
            if (keyBytes.length != 16 && keyBytes.length != 24 && keyBytes.length != 32) {
                throw new ParquetCryptoRuntimeException(
                        "Master key " + keyId + " must be 128, 192 or 256 bits, got "
                                + (keyBytes.length * 8));
            }
            masterKeys.put(keyId, keyBytes);
        }
    }

    @Override
    public String wrapKey(byte[] keyBytes, String masterKeyIdentifier)
            throws KeyAccessDeniedException {
        byte[] masterKey = requireMasterKey(masterKeyIdentifier);
        try {
            byte[] nonce = new byte[GCM_NONCE_BYTES];
            random.nextBytes(nonce);

            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(
                    Cipher.ENCRYPT_MODE,
                    new SecretKeySpec(masterKey, "AES"),
                    new GCMParameterSpec(GCM_TAG_BITS, nonce));
            cipher.updateAAD(masterKeyIdentifier.getBytes(StandardCharsets.UTF_8));
            byte[] ciphertext = cipher.doFinal(keyBytes);

            ByteBuffer envelope = ByteBuffer.allocate(nonce.length + ciphertext.length);
            envelope.put(nonce).put(ciphertext);
            return Base64.getEncoder().encodeToString(envelope.array());
        } catch (Exception e) {
            throw new ParquetCryptoRuntimeException(
                    "Failed to wrap data key with master key " + masterKeyIdentifier, e);
        }
    }

    @Override
    public byte[] unwrapKey(String wrappedKey, String masterKeyIdentifier)
            throws KeyAccessDeniedException {
        byte[] masterKey = requireMasterKey(masterKeyIdentifier);
        try {
            byte[] envelope = Base64.getDecoder().decode(wrappedKey);
            byte[] nonce = new byte[GCM_NONCE_BYTES];
            System.arraycopy(envelope, 0, nonce, 0, GCM_NONCE_BYTES);

            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(
                    Cipher.DECRYPT_MODE,
                    new SecretKeySpec(masterKey, "AES"),
                    new GCMParameterSpec(GCM_TAG_BITS, nonce));
            cipher.updateAAD(masterKeyIdentifier.getBytes(StandardCharsets.UTF_8));
            return cipher.doFinal(envelope, GCM_NONCE_BYTES, envelope.length - GCM_NONCE_BYTES);
        } catch (Exception e) {
            // Surfaced when the wrong master key is presented or the wrapped key has
            // been tampered with -- the GCM tag check fails. Experiment arm C of the
            // spike depends on this being an error rather than silent garbage.
            throw new KeyAccessDeniedException(
                    "Failed to unwrap data key with master key " + masterKeyIdentifier
                            + ": " + e.getMessage());
        }
    }

    private byte[] requireMasterKey(String masterKeyIdentifier) throws KeyAccessDeniedException {
        byte[] masterKey = masterKeys.get(masterKeyIdentifier);
        if (masterKey == null) {
            throw new KeyAccessDeniedException(
                    "Master key not available to this principal: " + masterKeyIdentifier);
        }
        return masterKey;
    }
}
